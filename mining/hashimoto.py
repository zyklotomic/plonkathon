#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Requirements:
- I/O bound: cycles spent on I/O â‰« cycles spent in cpu
- no sharding: impossible to implement data locality strategy
- easy verification

Thoughts:

Efficient implementations will not switch context (threading) when waiting for data.
But they would leverage all fill buffers and have concurrent memory accesses.
It can be assumed, that code can be written in a way to calculate N (<10)
nonces in parallel (on a single core).

So, after all maybe memory bandwidth rather than latency is the actual bottleneck.
Can this be solved in a way that aligns with hashing nonces and allows
for a quick verification? Probably not.

Loop unrolling:
Initially proposed dagger sets offer data locality which allows to scale the algo 
on multiple cores/l2chaches. 320MB / 40sets = 8MB (< L2 cache)
A solution is to make accessed mem location depended on the value of the
previous access.

Partitial Memory:
If a users only keeps e.g. one third of each DAG in memory (i.e. to 
have in L3 cache), he still can answer ~0.5**k of accesses by substituting 
them through previous node lookups. 
This can be mitigated by
a) making each node deterministically depend on the value of at
least one close high memory node. Optionally for quick validation, select
the 2nd dependency for the lower (cached) memory. see produce_dag_k2dr
b) for DAG creation, using a hashing function which needs more cycles
than multiple memory lookups would - even for GPUs/FPGAs/ASICs.
"""


try:
    shathree = __import__('sha3')
except:
    shathree = __import__('python_sha3')
import time


def sha3(x):
    return decode_int(shathree.sha3_256(x).digest()) #

def decode_int(s):
    o = 0
    for i in range(len(s)):
        o = o * 256 + ord(s[i])
    return o


def encode_int(x):
    o = ''
    for _ in range(64):
        o = chr(x % 256) + o
        x //= 256
    return o



def get_daggerset(params, seedset):
    return [produce_dag(params, i) for i in seedset]

def update_daggerset(params, daggerset, seedset, seed):
    idx = decode_int(seed) % len(daggerset)
    seedset[idx] = seed
    daggerset[idx] = produce_dag(params, seed)


P = (2**256 - 4294968273)**2


def produce_dag(params, seed):
    k, w, d = params.k, params.w, params.d
    o = [sha3(seed)**2]
    init = o[0]
    picker = 1
    for i in range(1, params.dag_size):
        x = 0
        picker = (picker * init) % P
        #assert picker == pow(init, i, P)
        curpicker = picker
        for j in range(k): # can be flattend if params are known
            pos = curpicker % i
            x |= o[pos]
            curpicker >>= 10
        o.append(pow(x, w, P))  # use any "hash function" here
    return o

def quick_calc(params, seed, pos, known={}):
    init = sha3(seed)**2
    k, w, d = params.k, params.w, params.d
    known[0] = init
    def calc(i):
        if i not in known:
            curpicker = pow(init, i, P)
            x = 0
            for j in range(k):
                pos = curpicker % i
                x |= calc(pos)
                curpicker >>= 10
            known[i] = pow(x, w, P)
        return known[i]
    o = calc(pos)
    return o


def produce_dag_k2dr(params, seed):
    """
    # k=2 and dependency ranges d  [:i/d], [-i/d:]
    Idea is to prevent partitial memory availability in
    which a significant part of the higher mem acesses
    can be substituted by two low mem accesses, plus some calc.
    """
    w, d = params.w, params.d
    o = [sha3(seed)**2]
    init = o[0]
    picker = 1
    for i in range(1, params.dag_size):
        x = 0
        picker = (picker * init) % P
        curpicker = picker
        # higher end
        f = i/d + 1
        pos = i - f + curpicker % f
        x |= o[pos]
        curpicker >>= 10
        # lower end
        pos = f - curpicker % f - 1
        x |= o[pos]
        o.append(pow(x, w, P))  # use any "hash function" here
    return o


def quick_calc_k2dr(params, seed, pos, known={}):
    # k=2 and dependency ranges d [:i/d], [-i/d:]
    init = sha3(seed) ** 2
    k, w, d = params.k, params.w, params.d
    known[0] = init
    def calc(i):
        if i not in known:
            curpicker = pow(init, i, P)
            x = 0
            # higher end
            f = i/d + 1
            pos = i - f + curpicker % f
            x |= calc(pos)
            curpicker >>= 10
            # lower end
            pos = f - curpicker % f - 1
            x |= calc(pos)
            known[i] = pow(x, w, P)
        return known[i]
    o = calc(pos)
    return o

produce_dag = produce_dag_k2dr
quick_calc = quick_calc_k2dr

def hashimoto(daggerset, lookups, header, nonce):
    """
    Requirements:
    - I/O bound: cycles spent on I/O â‰« cycles spent in cpu
    - no sharding: impossible to implement data locality strategy

    # I/O bound:
    e.g. lookups = 16
    sha3:       12 * 32   ~384 cycles
    lookups:    16 * 160 ~2560 cycles # if zero cache
    loop:       16 * 3     ~48 cycles
    I/O / cpu = 2560/432 = ~ 6/1

    # no sharding
    lookups depend on previous lookup results
    impossible to route computation/lookups based on the initial sha3
    """
    num_dags = len(daggerset)
    dag_size = len(daggerset[0])
    mix = sha3(header + encode_int(nonce)) ** 2
    # loop, that can not be unrolled
    # dag and dag[pos] depended on previous lookup
    for i in range(lookups):
        dag = daggerset[mix % num_dags] # modulo
        pos = mix % dag_size    # modulo
        mix ^= dag[pos]         # xor
    return mix

def light_hashimoto(params, seedset, header, nonce):
    lookups = params.lookups
    dag_size = params.dag_size
    known = dict((s, {}) for s in seedset) # cache results for each dag
    mix = sha3(header + encode_int(nonce)) ** 2 
    for i in range(lookups):
        seed = seedset[mix % len(seedset)]
        pos = mix % dag_size
        mix ^= quick_calc(params, seed, pos, known[seed])
    num_accesses = sum(len(known[s]) for s in seedset)
    print 'Calculated %d lookups with %d accesses' % (lookups, num_accesses)
    return mix

def light_verify(params, seedset, header, nonce):
    return light_hashimoto(params, seedset, header, nonce) \
        <= 2**512 / params.diff


def mine(daggerset, params, header, nonce=0):
    orignonce = nonce
    origtime = time.time()
    while 1:
        h = hashimoto(daggerset, params.lookups, header, nonce)
        if h <= 2**512 / params.diff:
            noncediff = nonce - orignonce
            timediff = time.time() - origtime
            print 'Found nonce: %d, tested %d nonces in %.2f seconds (%d per sec)' % \
                (nonce, noncediff, timediff, noncediff / timediff)
            return nonce
        nonce += 1


class params(object):
    """
    === tuning ===
    memory: memory requirements â‰« L2/L3/L4 cache sizes
    lookups:  hashes_per_sec(lookups=0) â‰« hashes_per_sec(lookups_mem_hard)
    k:        ?
    d:        higher values enfore memory availability but require more quick_calcs
    numdags:  so that a dag can be updated in reasonable time
    """
    memory = 512 * 1024**2          # memory usage
    numdags = 128                   # number of dags
    dag_size = memory /numdags / 64 # num 64byte values per dag
    lookups = 512                   # memory lookups per hash
    diff = 2**14                    # higher is harder
    k = 2                           # num dependecies of each dag value
    d = 8                           # max distance of first dependency (1/d=fraction of size)
    w = 2


if __name__ == '__main__':
    print dict((k,v) for k,v in params.__dict__.items() if isinstance(v,int))

    # odds of a partitial storage attack
    missing_mem = 0.01
    P_partitial_mem_success = (1-missing_mem) ** params.lookups
    print 'P success per hash with %d%% mem missing: %d%%' %(missing_mem*100, P_partitial_mem_success*100)

    # which actually only results in a slower mining, as more hashes must be tried
    slowdown = 1/ P_partitial_mem_success
    print 'x%.1f speedup required to offset %d%% missing mem' % (slowdown, missing_mem*100)

    # create set of DAGs
    st = time.time()
    seedset = [str(i) for i in range(params.numdags)]
    daggerset = get_daggerset(params, seedset)
    print 'daggerset with %d dags' % len(daggerset), 'size:', 64*params.dag_size*params.numdags / 1024**2 , 'MB'
    print 'creation took %.2fs' % (time.time() - st)

    # update DAG
    st = time.time()
    update_daggerset(params, daggerset, seedset, seed='new') 
    print 'updating 1 dag took %.2fs' % (time.time() - st)

    # Mine
    for i in range(10):
        header = 'test%d' % i
        print '\nmining', header
        nonce = mine(daggerset, params, header)
        # verify
        st = time.time()
        assert light_verify(params, seedset, header, nonce)
        print 'verification took %.2fs' % (time.time() - st)




