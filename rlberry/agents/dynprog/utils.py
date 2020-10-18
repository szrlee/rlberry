import numpy as np
from numba import jit
from typing import Tuple, Callable


@jit(nopython=True)
def backward_induction(R, P, horizon, gamma=1.0, vmax=np.inf):
    """
    Backward induction to compute Q and V functions in the finite horizon setting.

    Parameters
    ----------
    R : numpy.ndarray
        array of shape (S, A) contaning the rewards, where S is the number 
        of states and A is the number of actions
    P : numpy.ndarray
        array of shape (S, A, S) such that P[s,a,ns] is the probability of 
        arriving at ns by taking action a in state s. 
    horizon : int
        problem horizon
    gamma : double
        discount factor, default = 1.0
    vmax : double
        maximum possible value in V
        default = np.inf
    
    Returns
    --------
    tuple (Q, V) containing the Q and V functions, of shapes (horizon, S, A)
    and (horizon, S), respectively.
    """
    S, A = R.shape
    V = np.zeros((horizon, S))
    Q = np.zeros((horizon, S, A))
    for hh in range(horizon - 1, -1, -1):
        for ss in range(S):
            max_q = -np.inf
            for aa in range(A):
                q_aa = R[ss, aa]
                if hh < horizon - 1:
                    q_aa += gamma * P[ss, aa, :].dot(V[hh + 1, :])
                if q_aa > max_q:
                    max_q = q_aa
                Q[hh, ss, aa] = q_aa
            V[hh, ss] = max_q
            if V[hh, ss] > vmax:
                V[hh, ss] = vmax
    return Q, V


@jit(nopython=True)
def value_iteration(R, P, gamma, epsilon=1e-6):
    """
    Value iteration for discounted problems.

    Parameters
    ----------
    R : numpy.ndarray
        array of shape (S, A) contaning the rewards, where S is the number 
        of states and A is the number of actions
    P : numpy.ndarray
        array of shape (S, A, S) such that P[s,a,ns] is the probability of 
        arriving at ns by taking action a in state s. 
    gamma : double
        discount factor
    epsilon : double
        precision 

    Returns
    --------
    tuple (Q, V) containing the epsilon-optimal Q and V functions, 
    of shapes (S, A) and (S,), respectively.
    """
    S, A  = R.shape 
    Q     = np.zeros((S, A))
    Q_aux = np.full((S, A), np.inf)
    while np.abs(Q - Q_aux).max() > epsilon:
        Q_aux = Q
        Q = bellman_operator(Q, R, P, gamma)

    V  = np.zeros(S)
    # numba does not support np.max(Q, axis=1)
    for ss in range(S):
        V[ss] = Q[ss, :].max()  
    return Q, V 


@jit(nopython=True)
def bellman_operator(Q, R, P, gamma):
    """
    Bellman optimality operator for Q functions

    Parameters
    ----------
    Q : numpy.ndarray
        array of shape (S, A) containing the Q function to which apply
        the operator
    R : numpy.ndarray
        array of shape (S, A) contaning the rewards, where S is the number 
        of states and A is the number of actions
    P : numpy.ndarray
        array of shape (S, A, S) such that P[s,a,ns] is the probability of 
        arriving at ns by taking action a in state s. 
    gamma : double
        discount factor

    Returns
    --------
    TQ, array of shape (S, A) containing the result of the Bellman operator
    applied to the input Q
    """
    S, A = Q.shape 
    TQ = np.zeros((S, A))
    V  = np.zeros(S)
    # numba does not support np.max(Q, axis=1)
    for ss in range(S):
        V[ss] = Q[ss, :].max()
    #
    for ss in range(S):
        for aa in range(A):
            TQ[ss, aa] = R[ss, aa] + gamma*P[ss, aa, :].dot(V)
    return TQ


if __name__=='__main__':
    from rlberry.envs.finite import GridWorld 
    env = GridWorld()
    Q, V = backward_induction(env.R, env.P, 30, gamma=0.5)
    Q = Q[0, :, :]

    Q2, _ = value_iteration(env.R, env.P, 0.5)
