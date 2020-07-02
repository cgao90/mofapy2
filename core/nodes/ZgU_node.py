from __future__ import division
import numpy as np
import numpy.ma as ma
from copy import deepcopy
from mofapy2.core import gp_utils
from mofapy2.core import gpu_utils
from mofapy2.core.distributions import *
import scipy as s

# Import manually defined functions
from .variational_nodes import UnivariateGaussian_Unobserved_Variational_Node

#TODO :
#  could be integrated in usual Z node (using a check if U is in Markov blanket)
#  currentyl takes as N(0,1) prior for nonstuctured factors not flexible using pvar pmean

# ZgU_node
class ZgU_node(UnivariateGaussian_Unobserved_Variational_Node):
    """
     This nodes models Z in a model including inducing points U.
     As other updates and the ELBO only require marginal expectations of Z w.r.t q(Z,U) the node is a univariate variational distribution.
     Here we actually model q(z) = int q(z|u) q(u) du as their moment are required by all other nodes, the prior is not of importance and for simplicity set ot he variational distribution.
      """

    def __init__(self, dim, pmean, pvar, qmean, qvar, idx_inducing, qE=None, qE2=None, weight_views = False):
        super().__init__(dim=dim, pmean=pmean, pvar=pvar, qmean=qmean, qvar=qvar, qE=qE, qE2=qE2)

        self.mini_batch = None
        self.factors_axis = 1
        self.idx_inducing = idx_inducing
        self.Nu = len(self.idx_inducing)
        self.N = self.dim[0]
        self.K = self.dim[1]
        self.weight_views = weight_views

    def precompute(self, options):
        """ Method to precompute terms to speed up computation """
        gpu_utils.gpu_mode = options['gpu_mode']

    def removeFactors(self, idx, axis=0):
        super().removeFactors(idx, axis)
        self.K = self.dim[1]

    def get_mini_batch(self):
        """ Method to fetch minibatch """
        if self.mini_batch is None:
            return self.getExpectations()
        else:
            return self.mini_batch

    def updateParameters(self, ix=None, ro=1.):

        # Get expectations from other nodes
        U = self.markov_blanket["U"].getExpectations()

        assert "Sigma" in self.markov_blanket, "Sigma Node not found in Markov blanket of ZgU node."
        Sigma = self.markov_blanket['Sigma'].get_mini_batch()

        # Get variational parameters of current node
        Q = self.Q.getParameters()
        Qmean, Qvar = Q['mean'], Q['var']

        # Get expectations from other nodes
        W = self.markov_blanket["W"].getExpectations()
        Y = self.markov_blanket["Y"].get_mini_batch()
        tau = self.markov_blanket["Tau"].get_mini_batch()
        mask = [self.markov_blanket["Y"].nodes[m].getMask() for m in range(len(Y))]

        if "AlphaZ" in self.markov_blanket:
            Alpha = self.markov_blanket['AlphaZ'].get_mini_batch()
        else:
            Alpha = s.ones((self.N, self.K)) * 1.
            if ix is not None: Alpha = Alpha[ix,:]


        par_up = self._updateParameters(U, Alpha, Sigma, Qmean, Qvar, Y, W, tau, mask)

        # Update parameters
        if ix is None:
            Q['mean'] = par_up['Qmean']
            Q['var'] = par_up['Qvar']
        else:
            self.mini_batch['E'] = par_up['Qmean']
            self.mini_batch['E2'] = s.square(par_up['Qmean']) + par_up['Qvar']

            Q['mean'][ix,:] = par_up['Qmean']
            Q['var'][ix,:] = par_up['Qvar']

        self.Q.setParameters(mean=Q['mean'], var=Q['var'])  # NOTE should not be necessary but safer to keep for now
        self.P.setParameters(mean=Q['mean'], var=Q['var'])

    def _updateParameters(self, U, Alpha, Sigma, Qmean, Qvar, Y, W, tau, mask):
        """ Hidden method to compute parameter updates """

        K = self.dim[1]
        N = Sigma['cov'].shape[1]
        M = len(Y)

        # Masking
        for m in range(M):
            tau[m][mask[m]] = 0.

        weights = [1] * M
        if self.weight_views and M > 1:
            total_w = np.asarray([Y[m].shape[1] for m in range(M)]).sum()
            weights = np.asarray([total_w / (M * Y[m].shape[1]) for m in range(M)])
            weights = weights / weights.sum() * M

        # for non-structured factors take the standard updates for Z, ignoring U

        # Precompute terms to speed up GPU computation (only required for non-structured updates)
        foo = gpu_utils.array(s.zeros((N, K)))
        precomputed_bar = gpu_utils.array(s.zeros((N, K)))
        for m in range(M):
            tau_gpu = gpu_utils.array(tau[m])
            foo += weights[m] * gpu_utils.dot(tau_gpu, gpu_utils.array(W[m]["E2"]))
            bar_tmp1 = gpu_utils.array(W[m]["E"])
            bar_tmp2 = tau_gpu * gpu_utils.array(Y[m])
            precomputed_bar += weights[m] * gpu_utils.dot(bar_tmp2, bar_tmp1)
        foo = gpu_utils.asnumpy(foo)

        # Calculate updates
        for k in range(K):
            unstructured = (Sigma['cov'][k] == np.eye(N)).all() # #TODO find better ways to choose between sparse and non-sparse inference depending on factor smoothness?
            # unstructured = False
            if unstructured: # updates according to q(z) without sparse inference
                    bar = gpu_utils.array(s.zeros((N,)))
                    tmp_cp1 = gpu_utils.array(Qmean[:, s.arange(K) != k])
                    for m in range(M):
                        tmp_cp2 = gpu_utils.array(W[m]["E"][:, s.arange(K) != k].T)

                        bar_tmp1 = gpu_utils.array(W[m]["E"][:, k])
                        bar_tmp2 = gpu_utils.array(tau[m]) * (-gpu_utils.dot(tmp_cp1, tmp_cp2))

                        bar +=  weights[m] * gpu_utils.dot(bar_tmp2, bar_tmp1)
                    bar += precomputed_bar[:, k]
                    bar = gpu_utils.asnumpy(bar)

                    Qvar[:, k] = 1. / (1 + foo[:, k])
                    Qmean[:, k] = Qvar[:, k] * bar
            else: # updates according to p(z|u)
                SigmaZZ = Sigma['cov'][k]
                SigmaZZ = gpu_utils.dot(gpu_utils.dot(np.diag(np.sqrt(1 / Alpha[:, k])), SigmaZZ),
                                        np.diag(np.sqrt(1 / Alpha[:, k])))
                SigmaZU = SigmaZZ[:, self.idx_inducing]
                p_cov_inv = Sigma['inv'][k,:,:]
                p_cov_inv = gpu_utils.dot(gpu_utils.dot(np.diag(np.sqrt(Alpha[self.idx_inducing, k])),p_cov_inv),
                                       np.diag(np.sqrt(Alpha[self.idx_inducing, k])))
                mat = gpu_utils.dot(SigmaZU, p_cov_inv)
                Qmean[:, k] = gpu_utils.dot(mat, U['E'][:,k])
                for n in range(N):
                    exp_var =  SigmaZZ[n, n] - gpu_utils.dot(gpu_utils.dot( SigmaZZ[n,self.idx_inducing], p_cov_inv),  SigmaZZ[self.idx_inducing, n])
                    var_exp = gpu_utils.dot(gpu_utils.dot(mat[n,:],U['cov'][k,:,:]), mat[n,:].transpose())
                    Qvar[n, k] = exp_var + var_exp

        # Save updated parameters of the Q distribution
        return {'Qmean': Qmean, 'Qvar': Qvar}

    def calculateELBO_k(self, k):
        # Collect parameters and expectations of current node
        Qpar, Qexp = self.Q.getParameters(), self.Q.getExpectations()
        Qmean, Qvar = Qpar['mean'], Qpar['var']
        QE, QE2 = Qexp['E'], Qexp['E2']

        if 'Sigma' in self.markov_blanket:
            Sigma = self.markov_blanket['Sigma'].getExpectations()
            p_cov = Sigma['cov']
        else:
            p_cov = self.P.params['cov']

        # only non-stucutred nodes contribute here, else p(z|u) = q(z|u) --> ELBO is zero
        unstructured = (p_cov[k] == np.eye(p_cov[k].shape[0])).all()
        # unstructured = False

        if unstructured:
            if 'AlphaZ' in self.markov_blanket:
                Alpha = self.markov_blanket['AlphaZ'].getExpectations(expand=True)
            else:
                Alpha = dict()
                Alpha['E'] = s.ones((self.N, self.K)) * 1.
                Alpha['lnE'] = s.zeros((self.N, self.K))

            tmp1 = -0.5 * (QE2[:,k] * Alpha['E'][:,k]).sum()
            tmp2 = 0.5 * Alpha["lnE"][:,k].sum()

            lb_p = tmp1 + tmp2
            lb_q = -(s.log(Qvar[:,k])).sum() + self.dim[0] / 2.

            return lb_p - lb_q - self.markov_blanket['U'].calculateELBO_k(k) # TODO check optim Sigma and avoid correction for ELBO of U node for unstructured node as U node doesn't contribute anymore

        else:
            return 0

    # sum up individual EBLO calculations (only non-structured factors contribute here instead of in U)
    def calculateELBO(self):
        elbo = 0
        for k in range(self.dim[1]):
            elbo += self.calculateELBO_k(k)
        return elbo

    # def sample(self):
    #     mu = self.P.params['mean']
    #     if "Sigma" in self.markov_blanket:
    #         Sigma = self.markov_blanket['Sigma']
    #         cov = Sigma.sample()
    #     else:
    #         cov = self.P.params['cov']
    #
    #     samp_tmp = [s.random.multivariate_normal(mu[:, i], cov[i, :, :]) for i in range(self.dim[1])]
    #     self.samp = s.array([tmp - tmp.mean() for tmp in samp_tmp]).transpose()
    #     return self.samp
