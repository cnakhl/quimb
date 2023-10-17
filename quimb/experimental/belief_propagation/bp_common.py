import functools
import operator

import autoray as ar

import quimb.tensor as qtn


class RollingDiffMean:
    """Tracker for the absolute rolling mean of diffs between values, to
    assess effective convergence of BP above actual message tolerance.
    """

    def __init__(self, size=16):
        self.size = size
        self.diffs = []
        self.last_x = None
        self.dxsum = 0.0

    def update(self, x):
        if self.last_x is not None:
            dx = x - self.last_x
            self.diffs.append(dx)
            self.dxsum += dx / self.size
        if len(self.diffs) > self.size:
            dx = self.diffs.pop(0)
            self.dxsum -= dx / self.size
        self.last_x = x

    def absmeandiff(self):
        if len(self.diffs) < self.size:
            return float("inf")
        return abs(self.dxsum)


class BeliefPropagationCommon:
    """Common interfaces for belief propagation algorithms.

    Parameters
    ----------
    max_iterations : int, optional
        The maximum number of iterations to perform.
    tol : float, optional
        The convergence tolerance for messages.
    progbar : bool, optional
        Whether to show a progress bar.
    """

    def run(self, max_iterations=1000, tol=5e-6, progbar=False):
        if progbar:
            import tqdm

            pbar = tqdm.tqdm()
        else:
            pbar = None

        try:
            it = 0
            rdm = RollingDiffMean()
            self.converged = False
            while not self.converged and it < max_iterations:
                # can only converge if tol > 0.0
                nconv, ncheck, max_mdiff = self.iterate(tol=tol)
                rdm.update(max_mdiff)
                self.converged = (max_mdiff < tol) or (rdm.absmeandiff() < tol)
                it += 1

                if pbar is not None:
                    pbar.set_description(
                        f"nconv: {nconv}/{ncheck} max|dM|={max_mdiff:.2e}",
                        refresh=False,
                    )
                    pbar.update()

        finally:
            if pbar is not None:
                pbar.close()

        if tol != 0.0 and not self.converged:
            import warnings

            warnings.warn(
                f"Belief propagation did not converge after {max_iterations} "
                f"iterations, tol={tol:.2e}, max|dM|={max_mdiff:.2e}."
            )


def prod(xs):
    """Product of all elements in ``xs``."""
    return functools.reduce(operator.mul, xs)


def initialize_hyper_messages(
    tn,
    fill_fn=None,
    smudge_factor=1e-12
):
    """Initialize messages for belief propagation, this is equivalent to doing
    a single round of belief propagation with uniform messages.

    Parameters
    ----------
    tn : TensorNetwork
        The tensor network to initialize messages for.
    fill_fn : callable, optional
        A function to fill the messages with, of signature ``fill_fn(shape)``.
    smudge_factor : float, optional
        A small number to add to the messages to avoid numerical issues.

    Returns
    -------
    messages : dict
        The initial messages. For every index and tensor id pair, there will
        be a message to and from with keys ``(ix, tid)`` and ``(tid, ix)``.
    """
    from quimb.tensor.contraction import array_contract

    backend = ar.infer_backend(next(t.data for t in tn))
    _sum = ar.get_lib_fn(backend, "sum")

    messages = {}

    # compute first messages from tensors to indices
    for tid, t in tn.tensor_map.items():
        k_inputs = tuple(range(t.ndim))
        for i, ix in enumerate(t.inds):
            if fill_fn is None:
                # sum over all other indices to get initial message
                m = array_contract(
                    arrays=(t.data,),
                    inputs=(k_inputs,),
                    output=(i,),
                )
                # normalize and insert
                messages[tid, ix] = m / _sum(m)
            else:
                d = t.ind_size(ix)
                m = fill_fn((d,))
                messages[tid, ix] = m / _sum(m)

    # compute first messages from indices to tensors
    for ix, tids in tn.ind_map.items():
        ms = [messages[tid, ix] for tid in tids]
        mp = prod(ms)
        for mi, tid in zip(ms, tids):
            m = mp / (mi + smudge_factor)
            # normalize and insert
            messages[ix, tid] = m / _sum(m)

    return messages


def combine_local_contractions(
    tvals,
    mvals,
    backend,
    strip_exponent=False,
    check_for_zero=True,
):
    _abs = ar.get_lib_fn(backend, "abs")
    _log10 = ar.get_lib_fn(backend, "log10")

    mantissa = 1
    exponent = 0
    for vt in tvals:
        avt = _abs(vt)

        if check_for_zero and (avt == 0.0):
            if strip_exponent:
                return 0.0, 0.0
            else:
                return 0.0

        mantissa = mantissa * (vt / avt)
        exponent = exponent + _log10(avt)
    for mt in mvals:
        amt = _abs(mt)
        mantissa = mantissa / (mt / amt)
        exponent = exponent - _log10(amt)

    if strip_exponent:
        return mantissa, exponent
    else:
        return mantissa * 10**exponent


def maybe_get_thread_pool(thread_pool):
    """Get a thread pool if requested."""
    if thread_pool is False:
        return None

    if thread_pool is True:
        import quimb as qu

        return qu.get_thread_pool()

    if isinstance(thread_pool, int):
        import quimb as qu

        return qu.get_thread_pool(thread_pool)

    return thread_pool


def create_lazy_community_edge_map(tn, site_tags=None, rank_simplify=True):
    """For lazy BP algorithms, create the data structures describing the
    effective graph of the lazily grouped 'sites' given by ``site_tags``.
    """
    if site_tags is None:
        site_tags = set(tn.site_tags)
    else:
        site_tags = set(site_tags)

    edges = {}
    neighbors = {}
    local_tns = {}
    touch_map = {}

    for ix in tn.ind_map:
        ts = tn._inds_get(ix)
        tags = {tag for t in ts for tag in t.tags if tag in site_tags}
        if len(tags) >= 2:
            i, j = tuple(sorted(tags))

            if (i, j) in edges:
                # already processed this edge
                continue

            # add to neighbor map
            neighbors.setdefault(i, []).append(j)
            neighbors.setdefault(j, []).append(i)

            # get local TNs and compute bonds between them,
            # rank simplify here also to prepare for contractions
            try:
                tn_i = local_tns[i]
            except KeyError:
                tn_i = local_tns[i] = tn.select(i, virtual=False)
                if rank_simplify:
                    tn_i.rank_simplify_()
            try:
                tn_j = local_tns[j]
            except KeyError:
                tn_j = local_tns[j] = tn.select(j, virtual=False)
                if rank_simplify:
                    tn_j.rank_simplify_()

            edges[i, j] = tuple(qtn.bonds(tn_i, tn_j))

    for i, j in edges:
        touch_map[(i, j)] = tuple((j, k) for k in neighbors[j] if k != i)
        touch_map[(j, i)] = tuple((i, k) for k in neighbors[i] if k != j)

    if len(local_tns) != len(site_tags):
        # handle potentially disconnected sites
        for i in sorted(site_tags):
            try:
                tn_i = local_tns[i] = tn.select(i, virtual=False)
                if rank_simplify:
                    tn_i.rank_simplify_()
            except KeyError:
                pass

    return edges, neighbors, local_tns, touch_map
