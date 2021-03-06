from cvxopt.base import matrix
import numpy as np
from scipy import optimize as op
import sklearn.cluster as cl

__author__ = 'nicococo'


class AbstractTCRFR(object):
    """ Basic functions for the Transductive Conditional Random Field Regression.

        Written by Nico Goernitz, TU Berlin, 2015
    """
    data = None             # (either matrix or list) data
    labels = None           # (list or matrix or array) labels
    label_inds = None       # index of corresponding data object for each label
    unlabeled_inds = None   # indices for unlabeled examples

    latent_prev = None   # (#V in {0,...,S-1}) previous latent states
    latent = None        # (#V in {0,...,S-1}) latent states (1-to-1 correspondence to data/labels object)
    latent_fixed = None  # (#V int) '1':corresponding state in 'latent' is fixed

    samples = -1  # (scalar) number of training data samples
    feats = -1    # (scalar) number of features != get_num_dims() !!!

    reg_lambda = 0.001  # (scalar) the regularization constant > 0
    reg_gamma = 1.0     # (scalar) crf regularizer
    reg_theta = 0.5     # (scalar) 0<= theta <= 1: trade-off between density estimation (0.0) and regression (1.0)

    v = None  # parameter vector of the crf (consisting of transition matrices and emission matrices)
    u = None  # parameter vector of the regression part

    A = None  # (Nodes x Nodes) = (#V x #V) sparse connectivity matrix (use scipy lil_matrix)
    S = -1    # number of discrete states for each node {0,..,S-1}

    V = None  # list of vertices in the graph (according to network structure matrix A)
    E = None  # list of tupels of transitions from edge i to edge j and transition matrix type

    N = None  # matrix of neighbors for each vertex
    N_weights = None

    Q = None  # (dims x dims) Crf regularization matrix

    trans_sym = None    # (trans_types {0,1} vector) '1':Transition matrix is symmetric,
                        # i.e. learn only S(S-1)/2 parameters instead of S*S, related to unpack_v
    trans_n = 0         # (scalar) number of transition matrices used (= max(A))

    trans_mtx2vec_full = None   # (S x S -> {0,...,S*S-1}) helper for converting full 2d transition
                                # matrices into 1d vectors (e.g. (1, 2) -> 3)
    trans_mtx2vec_sym = None    # (S x S) helper for converting symmetric 2d transition
                                # matrices into 1d vectors (e.g. (1, 2) -> 3)
    trans_vec2vec_mtx = None    # Linear transformation (matrix) converting a packed symmetric
                                # transition vector into an unpacked transition vector
                                # (\in {0,1}^(states*states x np.round(states*(states-1)/2 +states))
    trans_total_dims = -1         # (scalar) start of the emission scores in the final weight vector

    trans_regs = None   # (vector) \in R^trans_num_types, regularizer for transition matrices

    trans_d_sym = 0   # (scalar) number of values that need to be stored for a symmetric transition matrix
    trans_d_full = 0  # (scalar) number of values that need to be stored for a full transition matrix


    def __init__(self, data, labels, label_inds, unlabeled_inds, states, A,
                 reg_theta=0.5, reg_lambda=0.001, reg_gamma=1.0, trans_regs=[1.0], trans_sym=[1]):
        # sparse connectivity matrix (numbers indicate the type of connection = id of transition matrix)
        self.A = A
        (verts, foo) = A.size

        # transition types
        self.trans_n = np.int(max(A))

        # number of states that is used for all transition/emission matrices
        self.S = states
        self.latent = np.zeros(verts, dtype='i')
        self.latent_prev = np.zeros(verts, dtype='i')
        self.latent_fixed = np.zeros(verts, dtype='i')

        # some transition inits
        self.trans_d_sym = np.round(self.S * (self.S - 1.) / 2. + self.S)
        self.trans_d_full = np.round(self.S * self.S)
        # mark transition matrices as symmetric
        if len(trans_sym) == 1:
            self.trans_sym = trans_sym[0]*np.ones(self.trans_n, dtype='i')
        else:
            self.trans_sym = trans_sym
        # transition matrix regularization
        if len(trans_regs) == 1:
            self.trans_regs = trans_regs[0]*np.ones(self.trans_n, dtype='i')
        else:
            self.trans_regs = trans_regs
        self.trans_mtx2vec_full, self.trans_mtx2vec_sym, self.trans_vec2vec_mtx = self.get_trans_converters()

        n_sym_mtx = np.sum(self.trans_sym)
        self.trans_total_dims = np.int(n_sym_mtx * self.trans_d_sym + (self.trans_n - n_sym_mtx) * self.trans_d_full)

        # construct edge matrix
        num_edges = int((matrix(1.0, (1,A.size[0]))*A*matrix(1.0, (A.size[0],1)))[0]/2)
        self.V = range(verts)
        idx = 0
        self.E = np.zeros((num_edges, 2), dtype=np.int)
        for s in range(verts):
            for n in range(s, verts):
                if A[s, n] > 0:
                    self.E[idx, :] = (s, n)
                    idx += 1


        # neighbor list for all vertices
        max_conn = max(A*matrix(1.0, (A.size[0],1)))

        print max_conn
        self.N = np.zeros((len(self.V), max_conn), dtype='i')
        self.N_weights = np.ones((len(self.V), max_conn), dtype='i')
        for ind in self.V:
            ninds = np.where(np.array(matrix(A[ind, :]), dtype='i').reshape(A.size[0]) >= 1)[0]
            lens = ninds.size
            self.N[ind, :lens] = ninds
            if lens < max_conn:
                self.N[ind, lens:] = 0
                self.N_weights[ind, lens:] = 0.0

        # regularization constants
        self.reg_lambda = reg_lambda
        self.reg_gamma = reg_gamma
        self.reg_theta = reg_theta

        # check the data
        self.data = data
        self.labels = np.array(labels)
        self.label_inds = np.array(label_inds)
        self.unlabeled_inds = np.array(unlabeled_inds)
        # assume either co.matrix or list-of-objects
        if isinstance(data, matrix):
            self.feats, self.samples = data.size
            self.isListOfObjects = False
        elif isinstance(data, np.ndarray):
            self.feats, self.samples = data.shape
            self.isListOfObjects = False
        else:
            raise Exception("Could not recognize input data format.")

        # init crf-regularization matrix
        self.init_Q()

        # print some stats
        self.print_stats()

    def print_stats(self):
        # output some stats
        n_sym_mtx = np.sum(self.trans_sym)
        n_trans = np.int(n_sym_mtx * self.trans_d_sym + (self.trans_n - n_sym_mtx) * self.trans_d_full)
        n_em = self.S * self.get_num_feats()
        print('')
        print('===============================')
        print('TCRFR Properties:')
        print('===============================')
        print('- Samples       : {0}'.format(self.samples))
        print('- Labeled       : {0}'.format(self.label_inds.size))
        print('- Unlabeled     : {0}'.format(self.unlabeled_inds.size))
        print('- Features      : {0}'.format(self.feats))
        print('- CRF Dims      : {0} = {1}+{2}'.format(self.get_num_compressed_dims(), n_trans, n_em))
        print('-------------------------------')
        print('- Lambda        : {0}'.format(self.reg_lambda))
        print('- Gamma         : {0}'.format(self.reg_gamma))
        print('- Theta         : {0}'.format(self.reg_theta))
        print('- Q_regs        : {0}'.format(self.trans_regs))
        print('-------------------------------')
        print('- Edges         : {0}'.format(len(self.E)))
        print('- States        : {0}'.format(self.S))
        print('- Trans-types   : {0}'.format(self.trans_n))
        print('- Trans-Sym     : {0}'.format(self.trans_sym))
        # print('-------------------------------')
        # print('- Trans-Sym V2V : \n{0}'.format(self.trans_vec2vec_mtx))
        print('===============================')
        print('')

    def init_Q(self):
        # build the crf regularization matrix
        dims = self.trans_n*self.trans_d_full + self.S*self.feats
        foo = np.ones(dims)
        cnt = 0
        for i in range(self.trans_n):
            foo[cnt:cnt+self.trans_d_full] = 1.0
            for s in range(self.S):
                idx = self.trans_mtx2vec_full[s, s]
                foo[cnt:cnt+idx] = self.trans_regs[i]
            cnt += self.trans_d_full
        self.Q = np.diag(self.reg_gamma * foo)

    def get_trans_converters(self):
        # P: states x states -> states*states
        P = np.zeros((self.S, self.S))
        cnt = 0
        for s1 in range(self.S):
            for s2 in range(self.S):
                P[s1, s2] = cnt
                cnt += 1
        # R: states x states -> np.round(states*(states-1)/2 +states)
        R = np.zeros((self.S, self.S))
        cnt = 0
        for s1 in range(self.S):
            for s2 in range(s1, self.S):
                R[s1, s2] = cnt
                R[s2, s1] = cnt
                cnt += 1
        # vector of symmetric transitions to unpacked vector of transitions
        # M: np.round(states*(states-1)/2 +states) -> states*states
        # M \in {0,1}^(states*states x np.round(states*(states-1)/2 +states))
        N_sym = np.int(self.S*(self.S-1.)/2. + self.S)
        M = np.zeros((self.S*self.S, N_sym))
        row = 0
        for s1 in range(self.S):
            for s2 in range(self.S):
                M[row, R[s1, s2]] = 1.
                row += 1
        return P, R, M

    def em_estimate_v_obj_callback(self, v, psi, boolean):
        vn = self.unpack_v(v)
        return .5 * vn.T.dot(self.Q.dot(vn)) - vn.T.dot(psi) + self.log_partition(vn)

    def em_estimate_v_grad_callback(self, v, psi, boolean):
        vn = self.unpack_v(v)
        start = self.reg_gamma * vn
        grad_log_part = self.log_partition_derivative(vn)
        # print grad_log_part
        # print psi.size
        # print grad_log_part.size
        return start - psi + grad_log_part

    def em_estimate_v(self, v, psi, use_grads=True):
        vstar = v
        if use_grads:
            res = op.minimize(self.em_estimate_v_obj_callback, jac=self.em_estimate_v_grad_callback,
                              x0=vstar, args=(psi, True), method='L-BFGS-B')
        else:
            res = op.minimize(self.em_estimate_v_obj_callback, x0=vstar, args=(psi, True), method='L-BFGS-B')
        # print res.nfev, ' - ', res.nit, ' - ', res.fun
        # print self.unpack_v(res.x)
        return res.fun, res.x

    def em_estimate_u(self, X):
        y = self.labels
        # solve the ridge regression problem
        E = np.zeros((X.shape[1], X.shape[1]))
        np.fill_diagonal(E, self.reg_lambda)
        XXt = X.T.dot(X) + E
        XtY = (X.T.dot(y))
        if XXt.size > 1:
            u = np.linalg.inv(XXt).dot(XtY)
        else:
            u = 1.0 / XXt * XtY
        obj = self.reg_lambda / 2.0 * u.dot(u) + y.dot(y) / 2.0 - u.dot(X.T.dot(y)) + u.dot(X.T.dot(X.dot(u))) / 2.0
        return obj, u

    def fit(self, max_iter=50, hotstart=None, use_grads=True):
        u, v = self.get_hotstart()
        if hotstart is not None:
            print('Manual hotstart position defined.')
            u, v = hotstart

        obj = 1e09
        cnt_iter = 0
        is_converged = False

        # best objective, u and v
        best_sol = [0, 1e14, None, None, None]

        # terminate if objective function value doesn't change much
        while cnt_iter < max_iter and not is_converged:
            # 1. infer the latent states given the current intermediate solutions u and v
            phis, psi = self.map_inference(u, self.unpack_v(v))

            lats = ''
            for i in range(self.latent.size):
                lats += '{0}'.format(self.latent[i])
                if i in self.label_inds:
                    lats += '.'
                else:
                    lats += ' '
                lats += ' '
                if i % 50 == 0:
                    lats += '\n'
 #           print lats

            # 2. solve the crf parameter estimation problem
            obj_crf, v = self.em_estimate_v(v, psi, use_grads=use_grads)
            # 3. estimate new regression parameters
            obj_regression, u = self.em_estimate_u(phis[:, self.label_inds].T)
            # 4.a. check termination based on objective function progress
            old_obj = obj
            obj = self.reg_theta * obj_regression + (1.0 - self.reg_theta) * obj_crf
            rel = np.abs((old_obj - obj) / obj)
#            print('Iter={0} regr={1:4.2f} crf={2:4.2f}; objective={3:4.2f} rel={4:2.4f} lats={5}'.format(
#                cnt_iter, obj_regression, obj_crf, obj, rel, np.unique(self.latent).size))
            if best_sol[1] >= obj:
                best_sol = [cnt_iter, obj, u, v, self.latent]
#                print('*')
            if cnt_iter > 3 and rel < 0.0001:
                is_converged = True
            if np.isinf(obj) or np.isnan(obj):
                return False
            cnt_iter += 1
        iter, _, self.u, self.v, self.latent = best_sol
#        print('Take best solution from iteration {0}/{1}.'.format(iter, cnt_iter-1))

        # print
        vup = self.unpack_v(self.v)

        cnt = 0
        for i in range(self.trans_n):
#            print i
#            print vup[cnt:cnt+self.S*self.S].reshape((self.S, self.S), order='C')
            cnt += self.trans_d_full

#        print 'Emissions:'
#        print vup[cnt:]

        return is_converged

    def predict(self, lats=None):
        if lats is None:
            lats = self.latent
        # for debugging only
        phis = np.zeros((self.S*self.feats, self.samples))
        for s in range(self.S):
            inds = np.where(lats == s)[0]
            phis[s*self.feats:(s+1)*self.feats, inds] = self.data[:, inds]
        return self.u.dot(phis), lats

    def get_joint_feature_maps(self):
        # Regression Joint Feature Map
        phis = np.zeros((self.S*self.feats, self.samples))
        for s in range(self.S):
            inds = np.where(self.latent == s)[0]
            phis[s*self.feats:(s+1)*self.feats, inds] = self.data[:, inds]
        return phis, self.get_crf_joint_feature_map()

    def get_crf_joint_feature_map(self, sample=None):
        if sample is not None:
            y = sample
        else:
            y = self.latent
        psi = np.zeros(self.get_num_dims())
        # Transitions
        for e in self.E:
            yi = y[e[0]]
            yj = y[e[1]]
            etype = self.A[e[0], e[1]]
            etype_offset = (etype-1)*self.trans_d_full
            trans_idx = self.trans_mtx2vec_full[yi, yj]
            psi[trans_idx + etype_offset] += 1.0
        # Emissions
        feats = self.get_num_feats()
        cnt = self.trans_n*self.trans_d_full
        for v in self.V:
            psi[cnt+y[v]*feats:cnt+y[v]*feats+feats] += self.data[:, v]
        return psi

    def get_latent_diff(self):
        if self.latent is None:
            return -1
        if self.latent_prev is None:
            return 1e10
        return np.sum(np.abs(self.latent - self.latent_prev))

    def unpack_v(self, v):
        upv = np.zeros(self.trans_n*self.trans_d_full + self.S * self.get_num_feats())
        # transitions include various transition matrices, each either symmetric or full
        cnt = 0
        cnt_full = 0
        for i in range(self.trans_n):
            if self.trans_sym[i] == 1:
                # print ".................................................."
                # print v[cnt:cnt+self.trans_d_sym]
                # print self.trans_vec2vec_mtx.dot(v[cnt:cnt+self.trans_d_sym]).reshape((self.S, self.S), order='C')
                # print ".................................................."
                upv[cnt_full:cnt_full+self.trans_d_full] = self.trans_vec2vec_mtx.dot(v[cnt:cnt+self.trans_d_sym])
                cnt += self.trans_d_sym
            else:
                upv[cnt_full:cnt_full+self.trans_d_full] = v[cnt:cnt+self.trans_d_full]
                cnt += self.trans_d_full
            cnt_full += self.trans_d_full
        # emissions
        upv[cnt_full:] = v[cnt:]
        return upv

    solution_latent = None

    def get_hotstart(self):
        # initialize all non-fixed latent variables with random states
        inds = np.where(self.latent_fixed == 0)[0]
        #self.latent[inds] = np.random.randint(self.S, size=inds.size)
        kmeans = cl.KMeans(n_clusters=self.S, init='random', n_init=10, max_iter=100, tol=0.0001)
        kmeans.fit(self.data.T)
        self.latent = kmeans.labels_

        # self.latent = self.solution_latent
        # print self.latent

        phis, psi = self.get_joint_feature_maps()
        # point in the direction of psi (unpacked)
        _, v = self.em_estimate_v(np.zeros(self.get_num_compressed_dims()), psi, use_grads=False)
        # v = psi/100.0

        # estimate regression parameters
        _, u = self.em_estimate_u(phis[:, self.label_inds].T)
        return u, v

    def get_num_compressed_dims(self):
        # number of symmetric transition matrices
        n_sym_mtx = np.sum(self.trans_sym)
        return np.int(n_sym_mtx * self.trans_d_sym + (self.trans_n - n_sym_mtx) * self.trans_d_full + self.S * self.get_num_feats())

    def get_num_dims(self):
        # number of unpacked dimensions
        return self.trans_n*self.trans_d_full + self.S * self.get_num_feats()

    def get_num_labeled(self):
        return len(self.labels)

    def get_num_unlabeled(self):
        return len(self.unlabeled_inds)

    def get_num_samples(self):
        return self.samples

    def get_num_feats(self):
        return self.feats

    def map_inference(self, u, v):
        pass

    def log_partition(self, v):
        pass

    def log_partition_derivative(self, v):
        pass
