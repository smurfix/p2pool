from __future__ import division

import random
import time
import os

from twisted.python import log

import p2pool
from p2pool import skiplists
from p2pool.bitcoin import data as bitcoin_data, script
from p2pool.util import math, forest


share_data_type = bitcoin_data.ComposedType([
    ('previous_share_hash', bitcoin_data.PossiblyNoneType(0, bitcoin_data.IntType(256))),
    ('coinbase', bitcoin_data.VarStrType()),
    ('nonce', bitcoin_data.VarStrType()),
    ('new_script', bitcoin_data.VarStrType()),
    ('subsidy', bitcoin_data.IntType(64)),
    ('donation', bitcoin_data.IntType(16)),
    ('stale_info', bitcoin_data.IntType(8)), # 0 nothing, 253 orphan, 254 doa. previously: 254*perfect_round(my_stale_prop), None if no shares
])

share_info_type = bitcoin_data.ComposedType([
    ('share_data', share_data_type),
    ('bits', bitcoin_data.FloatingIntegerType()),
    ('timestamp', bitcoin_data.IntType(32)),
])

share1a_type = bitcoin_data.ComposedType([
    ('header', bitcoin_data.block_header_type),
    ('share_info', share_info_type),
    ('merkle_branch', bitcoin_data.merkle_branch_type),
])

share1b_type = bitcoin_data.ComposedType([
    ('header', bitcoin_data.block_header_type),
    ('share_info', share_info_type),
    ('other_txs', bitcoin_data.ListType(bitcoin_data.tx_type)),
])

# type:
# 0: share1a
# 1: share1b

share_type = bitcoin_data.ComposedType([
    ('type', bitcoin_data.VarIntType()),
    ('contents', bitcoin_data.VarStrType()),
])

class Share(object):
    __slots__ = 'header previous_block share_info merkle_branch other_txs timestamp share_data new_script subsidy previous_hash previous_share_hash target nonce pow_hash header_hash hash time_seen peer donation net'.split(' ')
    
    @classmethod
    def from_share(cls, share, net):
        if share['type'] == 0:
            res = cls.from_share1a(share1a_type.unpack(share['contents']), net)
            if not (res.pow_hash > res.header['bits'].target):
                raise ValueError('invalid share type')
            return res
        elif share['type'] == 1:
            res = cls.from_share1b(share1b_type.unpack(share['contents']), net)
            if not (res.pow_hash <= res.header['bits'].target):
                raise ValueError('invalid share type')
            return res
        else:
            raise ValueError('unknown share type: %r' % (share['type'],))
    
    @classmethod
    def from_share1a(cls, share1a, net):
        return cls(net, **share1a)
    
    @classmethod
    def from_share1b(cls, share1b, net):
        return cls(net, **share1b)
    
    def __init__(self, net, header, share_info, merkle_branch=None, other_txs=None):
        self.net = net
        
        if merkle_branch is None and other_txs is None:
            raise ValueError('need either merkle_branch or other_txs')
        if other_txs is not None:
            new_merkle_branch = bitcoin_data.calculate_merkle_branch([0] + map(bitcoin_data.tx_type.hash256, other_txs), 0)
            if merkle_branch is not None:
                if merke_branch != new_merkle_branch:
                    raise ValueError('invalid merkle_branch and other_txs')
            merkle_branch = new_merkle_branch
        
        if len(merkle_branch) > 16:
            raise ValueError('merkle_branch too long!')
        
        self.header = header
        self.previous_block = header['previous_block']
        self.share_info = share_info
        self.merkle_branch = merkle_branch
        
        self.share_data = self.share_info['share_data']
        self.target = self.share_info['bits'].target
        self.timestamp = self.share_info['timestamp']
        
        self.new_script = self.share_data['new_script']
        self.subsidy = self.share_data['subsidy']
        self.donation = self.share_data['donation']
        
        if len(self.new_script) > 100:
            raise ValueError('new_script too long!')
        
        self.previous_hash = self.previous_share_hash = self.share_data['previous_share_hash']
        self.nonce = self.share_data['nonce']
        
        if len(self.nonce) > 100:
            raise ValueError('nonce too long!')
        
        if len(self.share_data['coinbase']) > 100:
            raise ValueError('''coinbase too large! %i bytes''' % (len(self.share_data['coinbase']),))
        
        self.pow_hash = net.PARENT.POW_FUNC(header)
        self.header_hash = bitcoin_data.block_header_type.hash256(header)
        
        self.hash = share1a_type.hash256(self.as_share1a())
        
        if self.pow_hash > self.target:
            print 'hash %x' % self.pow_hash
            print 'targ %x' % self.target
            raise ValueError('not enough work!')
        
        self.other_txs = other_txs if self.pow_hash <= self.header['bits'].target else None
        
        # XXX eww
        self.time_seen = time.time()
        self.peer = None
    
    def __repr__(self):
        return '<Share %s>' % (' '.join('%s=%r' % (k, getattr(self, k)) for k in self.__slots__),)
    
    def check(self, tracker):
        if script.get_sigop_count(self.new_script) > 1:
            raise ValueError('too many sigops!')
        
        share_info, gentx = generate_transaction(tracker, self.share_info['share_data'], self.header['bits'].target, self.share_info['timestamp'], self.net)
        if share_info != self.share_info:
            raise ValueError('share difficulty invalid')
        
        if bitcoin_data.check_merkle_branch(bitcoin_data.tx_type.hash256(gentx), 0, self.merkle_branch) != self.header['merkle_root']:
            raise ValueError('''gentx doesn't match header via merkle_branch''')
    
    def as_share(self):
        if self.pow_hash > self.header['bits'].target: # share1a
            return dict(type=0, contents=share1a_type.pack(self.as_share1a()))
        elif self.pow_hash <= self.header['bits'].target: # share1b
            return dict(type=1, contents=share1b_type.pack(self.as_share1b()))
        else:
            raise AssertionError()
    
    def as_share1a(self):
        return dict(header=self.header, share_info=self.share_info, merkle_branch=self.merkle_branch)
    
    def as_share1b(self):
        if self.other_txs is None:
            raise ValueError('share does not contain all txs')
        
        return dict(header=self.header, share_info=self.share_info, other_txs=self.other_txs)
    
    def as_block(self, tracker):
        if self.other_txs is None:
            raise ValueError('share does not contain all txs')
        
        share_info, gentx = generate_transaction(tracker, self.share_info['share_data'], self.header['bits'].target, self.share_info['timestamp'], self.net)
        assert share_info == self.share_info
        
        return dict(header=self.header, txs=[gentx] + self.other_txs)

def get_pool_attempts_per_second(tracker, previous_share_hash, dist):
    assert dist >= 2
    near = tracker.shares[previous_share_hash]
    far = tracker.shares[tracker.get_nth_parent_hash(previous_share_hash, dist - 1)]
    attempts = tracker.get_work(near.hash) - tracker.get_work(far.hash)
    time = near.timestamp - far.timestamp
    if time <= 0:
        time = 1
    return attempts//time

def get_average_stale_prop(tracker, share_hash, lookbehind):
    def stales_per_share(share):
        if share.share_data['stale_info'] == 253: # orphan
            return 1
        elif share.share_data['stale_info'] == 254: # doa
            return 1
        elif share.share_data['stale_info'] == 0:
            return 0
        elif share.share_data['stale_info'] == 255: # temporary hack until everyone uses new-style stale data
            return 0
        else:
            return 1/(254/share.share_data['stale_info'] - 1) # converts stales/shares to stales/nonstales
            # 0 and 254 case are taken care of above and this will soon be removed anyway
    stales = sum(stales_per_share(share) for share in tracker.get_chain(share_hash, lookbehind))
    return stales/(lookbehind + stales)

def generate_transaction(tracker, share_data, block_target, desired_timestamp, net):
    previous_share_hash = share_data['previous_share_hash']
    new_script = share_data['new_script']
    subsidy = share_data['subsidy']
    donation = share_data['donation']
    assert 0 <= donation <= 65535
    
    if len(share_data['coinbase']) > 100:
        raise ValueError('coinbase too long!')
    
    previous_share = tracker.shares[previous_share_hash] if previous_share_hash is not None else None
    
    chain_length = getattr(net, 'REAL_CHAIN_LENGTH_FUNC', lambda _: net.REAL_CHAIN_LENGTH)(previous_share.timestamp if previous_share is not None else None)
    
    height, last = tracker.get_height_and_last(previous_share_hash)
    assert height >= chain_length or last is None
    if height < net.TARGET_LOOKBEHIND:
        bits = bitcoin_data.FloatingInteger.from_target_upper_bound(net.MAX_TARGET)
    else:
        attempts_per_second = get_pool_attempts_per_second(tracker, previous_share_hash, net.TARGET_LOOKBEHIND)
        pre_target = 2**256//(net.SHARE_PERIOD*attempts_per_second) - 1
        pre_target2 = math.clip(pre_target, (previous_share.target*9//10, previous_share.target*11//10))
        pre_target3 = math.clip(pre_target2, (0, net.MAX_TARGET))
        bits = bitcoin_data.FloatingInteger.from_target_upper_bound(pre_target3)
    
    attempts_to_block = bitcoin_data.target_to_average_attempts(block_target)
    max_att = net.SPREAD * attempts_to_block
    
    this_att = min(bitcoin_data.target_to_average_attempts(bits.target), max_att)
    other_weights, other_total_weight, other_donation_weight = tracker.get_cumulative_weights(previous_share_hash, min(height, chain_length), 65535*max(0, max_att - this_att))
    assert other_total_weight == sum(other_weights.itervalues()) + other_donation_weight, (other_total_weight, sum(other_weights.itervalues()) + other_donation_weight)
    weights, total_weight, donation_weight = math.add_dicts({new_script: this_att*(65535-donation)}, other_weights), this_att*65535 + other_total_weight, this_att*donation + other_donation_weight
    assert total_weight == sum(weights.itervalues()) + donation_weight, (total_weight, sum(weights.itervalues()) + donation_weight)
    
    SCRIPT = '4104ffd03de44a6e11b9917f3a29f9443283d9871c9d743ef30d5eddcd37094b64d1b3d8090496b53256786bf5c82932ec23c3b74d9f05a6f95a8b5529352656664bac'.decode('hex')
    
    # 1 satoshi is always donated so that a list of p2pool generated blocks can be easily found by looking at the donation address
    amounts = dict((script, (subsidy-1)*(199*weight)//(200*total_weight)) for (script, weight) in weights.iteritems())
    amounts[new_script] = amounts.get(new_script, 0) + (subsidy-1)//200
    amounts[SCRIPT] = amounts.get(SCRIPT, 0) + (subsidy-1)*(199*donation_weight)//(200*total_weight)
    amounts[SCRIPT] = amounts.get(SCRIPT, 0) + subsidy - sum(amounts.itervalues()) # collect any extra satoshis :P
    
    if sum(amounts.itervalues()) != subsidy:
        raise ValueError()
    if any(x < 0 for x in amounts.itervalues()):
        raise ValueError()
    
    dests = sorted(amounts.iterkeys(), key=lambda script: (amounts[script], script))
    dests = dests[-4000:] # block length limit, unlikely to ever be hit
    
    share_info = dict(
        share_data=share_data,
        bits=bits,
        timestamp=math.clip(desired_timestamp, (previous_share.timestamp - 60, previous_share.timestamp + 60)) if previous_share is not None else desired_timestamp,
    )
    
    return share_info, dict(
        version=1,
        tx_ins=[dict(
            previous_output=None,
            sequence=None,
            script=share_data['coinbase'].ljust(2, '\x00'),
        )],
        tx_outs=[dict(value=0, script='\x20' + bitcoin_data.IntType(256).pack(share_info_type.hash256(share_info)))] + [dict(value=amounts[script], script=script) for script in dests if amounts[script]],
        lock_time=0,
    )


class OkayTrackerDelta(object):
    __slots__ = 'head height work my_count my_doa_count my_orphan_announce_count my_dead_announce_count tail'.split(' ')
    
    @classmethod
    def get_none(cls, element_id):
        return cls(element_id, 0, 0, 0, 0, 0, 0, element_id)
    
    @classmethod
    def from_element(cls, share):
        return cls(share.hash,
            1, bitcoin_data.target_to_average_attempts(share.target),
            1 if share.hash in cls.my_share_hashes else 0,
            1 if share.hash in cls.my_doa_share_hashes else 0,
            1 if share.hash in cls.my_share_hashes and share.share_data['stale_info'] == 253 else 0,
            1 if share.hash in cls.my_share_hashes and share.share_data['stale_info'] == 254 else 0,
        share.previous_hash)
    
    def __init__(self, head, height, work, my_count, my_doa_count, my_orphan_announce_count, my_dead_announce_count, tail):
        self.head, self.height, self.work, self.tail = head, height, work, tail
        self.my_count, self.my_doa_count, self.my_orphan_announce_count, self.my_dead_announce_count = my_count, my_doa_count, my_orphan_announce_count, my_dead_announce_count
    
    def __add__(self, other):
        assert self.tail == other.head
        return OkayTrackerDelta(self.head,
            self.height + other.height, self.work + other.work,
            self.my_count + other.my_count, self.my_doa_count + other.my_doa_count, self.my_orphan_announce_count + other.my_orphan_announce_count, self.my_dead_announce_count + other.my_dead_announce_count,
        other.tail)
    
    def __sub__(self, other):
        if self.head == other.head:
            return OkayTrackerDelta(other.tail, self.height - other.height, self.work - other.work,
                self.my_count - other.my_count, self.my_doa_count - other.my_doa_count, self.my_orphan_announce_count - other.my_orphan_announce_count, self.my_dead_announce_count - other.my_dead_announce_count,
            self.tail)
        elif self.tail == other.tail:
            return OkayTrackerDelta(self.head, self.height - other.height, self.work - other.work,
                self.my_count - other.my_count, self.my_doa_count - other.my_doa_count, self.my_orphan_announce_count - other.my_orphan_announce_count, self.my_dead_announce_count - other.my_dead_announce_count,
            other.head)
        else:
            raise AssertionError()

class OkayTracker(forest.Tracker):
    def __init__(self, net):
        forest.Tracker.__init__(self)
        self.net = net
        self.verified = forest.Tracker(delta_type=OkayTrackerDelta)
        self.verified.get_nth_parent_hash = self.get_nth_parent_hash # self is a superset of self.verified
        
        self.get_cumulative_weights = skiplists.WeightsSkipList(self)
    
    def attempt_verify(self, share):
        if share.hash in self.verified.shares:
            return True
        height, last = self.get_height_and_last(share.hash)
        if height < self.net.CHAIN_LENGTH + 1 and last is not None:
            raise AssertionError()
        try:
            share.check(self)
        except:
            log.err(None, 'Share check failed:')
            return False
        else:
            self.verified.add(share)
            return True
    
    def think(self, ht, previous_block):
        desired = set()
        
        # O(len(self.heads))
        #   make 'unverified heads' set?
        # for each overall head, attempt verification
        # if it fails, attempt on parent, and repeat
        # if no successful verification because of lack of parents, request parent
        bads = set()
        for head in set(self.heads) - set(self.verified.heads):
            head_height, last = self.get_height_and_last(head)
            
            for share in self.get_chain(head, head_height if last is None else min(5, max(0, head_height - self.net.CHAIN_LENGTH))):
                if self.attempt_verify(share):
                    break
                if share.hash in self.heads:
                    bads.add(share.hash)
            else:
                if last is not None:
                    desired.add((self.shares[random.choice(list(self.reverse_shares[last]))].peer, last))
        for bad in bads:
            assert bad not in self.verified.shares
            assert bad in self.heads
            if p2pool.DEBUG:
                print "BAD", bad
            self.remove(bad)
        
        # try to get at least CHAIN_LENGTH height for each verified head, requesting parents if needed
        for head in list(self.verified.heads):
            head_height, last_hash = self.verified.get_height_and_last(head)
            last_height, last_last_hash = self.get_height_and_last(last_hash)
            # XXX review boundary conditions
            want = max(self.net.CHAIN_LENGTH - head_height, 0)
            can = max(last_height - 1 - self.net.CHAIN_LENGTH, 0) if last_last_hash is not None else last_height
            get = min(want, can)
            #print 'Z', head_height, last_hash is None, last_height, last_last_hash is None, want, can, get
            for share in self.get_chain(last_hash, get):
                if not self.attempt_verify(share):
                    break
            if head_height < self.net.CHAIN_LENGTH and last_last_hash is not None:
                desired.add((self.verified.shares[random.choice(list(self.verified.reverse_shares[last_hash]))].peer, last_last_hash))
        if p2pool.DEBUG:
            print len(self.verified.tails), "tails:"
            for x in self.verified.tails:
                print format_hash(x), self.score(max(self.verified.tails[x], key=self.verified.get_height), ht)
        
        # decide best tree
        best_tail = max(self.verified.tails, key=lambda h: self.score(max(self.verified.tails[h], key=self.verified.get_height), ht)) if self.verified.tails else None
        # decide best verified head
        scores = sorted(self.verified.tails.get(best_tail, []), key=lambda h: (
            self.verified.get_work(self.verified.get_nth_parent_hash(h, min(5, self.verified.get_height(h)))),
            #self.verified.shares[h].peer is None,
            ht.get_height_rel_highest(self.verified.shares[h].previous_block),
            -self.verified.shares[h].time_seen
        ))
        
        
        if p2pool.DEBUG:
            print len(self.verified.tails), "chain tails and", len(self.verified.tails.get(best_tail, [])), 'chain heads. Top 10 heads:'
            if len(scores) > 10:
                print '    ...'
            for h in scores[-10:]:
                print '   ', format_hash(h), format_hash(self.verified.shares[h].previous_hash), (
                    self.verified.get_work(self.verified.get_nth_parent_hash(h, min(5, self.verified.get_height(h)))),
                    self.verified.shares[h].peer is None,
                    ht.get_height_rel_highest(self.verified.shares[h].previous_block),
                    -self.verified.shares[h].time_seen
                )
        
        # eat away at heads
        if scores:
            for i in xrange(1000):
                to_remove = set()
                for share_hash, tail in self.heads.iteritems():
                    if share_hash in scores[-5:]:
                        #print 1
                        continue
                    if self.shares[share_hash].time_seen > time.time() - 300:
                        #print 2
                        continue
                    if share_hash not in self.verified.shares and max(self.shares[after_tail_hash].time_seen for after_tail_hash in self.reverse_shares.get(tail)) > time.time() - 120: # XXX stupid
                        #print 3
                        continue
                    to_remove.add(share_hash)
                if not to_remove:
                    break
                for share_hash in to_remove:
                    self.remove(share_hash)
                    if share_hash in self.verified.shares:
                        self.verified.remove(share_hash)
                #print "_________", to_remove
        
        # drop tails
        for i in xrange(1000):
            to_remove = set()
            for tail, heads in self.tails.iteritems():
                if min(self.get_height(head) for head in heads) < 2*self.net.CHAIN_LENGTH + 10:
                    continue
                for aftertail in self.reverse_shares.get(tail, set()):
                    if len(self.reverse_shares[self.shares[aftertail].previous_hash]) > 1: # XXX
                        print "raw"
                        continue
                    to_remove.add(aftertail)
            if not to_remove:
                break
            # if removed from this, it must be removed from verified
            #start = time.time()
            for aftertail in to_remove:
                if self.shares[aftertail].previous_hash not in self.tails:
                    print "erk", aftertail, self.shares[aftertail].previous_hash
                    continue
                self.remove(aftertail)
                if aftertail in self.verified.shares:
                    self.verified.remove(aftertail)
            #end = time.time()
            #print "removed! %i %f" % (len(to_remove), (end - start)/len(to_remove))
        
        best = scores[-1] if scores else None
        
        if best is not None:
            best_share = self.verified.shares[best]
            if ht.get_height_rel_highest(best_share.header['previous_block']) < ht.get_height_rel_highest(previous_block) and best_share.header_hash != previous_block and best_share.peer is not None:
                if p2pool.DEBUG:
                    print 'Stale detected! %x < %x' % (best_share.header['previous_block'], previous_block)
                best = best_share.previous_hash
        
        return best, desired
    
    def score(self, share_hash, ht):
        head_height = self.verified.get_height(share_hash)
        if head_height < self.net.CHAIN_LENGTH:
            return head_height, None
        
        end_point = self.verified.get_nth_parent_hash(share_hash, self.net.CHAIN_LENGTH*15//16)
        
        block_height = max(ht.get_height_rel_highest(share.header['previous_block']) for share in
            self.verified.get_chain(end_point, self.net.CHAIN_LENGTH//16))
        
        return self.net.CHAIN_LENGTH, (self.verified.get_work(share_hash) - self.verified.get_work(end_point))//(0 - block_height + 1)

def format_hash(x):
    if x is None:
        return 'xxxxxxxx'
    return '%08x' % (x % 2**32)

class ShareStore(object):
    def __init__(self, prefix, net):
        self.filename = prefix
        self.dirname = os.path.dirname(os.path.abspath(prefix))
        self.filename = os.path.basename(os.path.abspath(prefix))
        self.net = net
        self.known = None # will be filename -> set of share hashes, set of verified hashes
        self.known_desired = None
    
    def get_shares(self):
        if self.known is not None:
            raise AssertionError()
        known = {}
        filenames, next = self.get_filenames_and_next()
        for filename in filenames:
            share_hashes, verified_hashes = known.setdefault(filename, (set(), set()))
            with open(filename, 'rb') as f:
                for line in f:
                    try:
                        type_id_str, data_hex = line.strip().split(' ')
                        type_id = int(type_id_str)
                        if type_id == 0:
                            pass
                        elif type_id == 1:
                            pass
                        elif type_id == 2:
                            verified_hash = int(data_hex, 16)
                            yield 'verified_hash', verified_hash
                            verified_hashes.add(verified_hash)
                        elif type_id == 5:
                            share = Share.from_share(share_type.unpack(data_hex.decode('hex')), self.net)
                            yield 'share', share
                            share_hashes.add(share.hash)
                        else:
                            raise NotImplementedError("share type %i" % (type_id,))
                    except Exception:
                        log.err(None, "Error while reading saved shares, continuing where left off:")
        self.known = known
        self.known_desired = dict((k, (set(a), set(b))) for k, (a, b) in known.iteritems())
    
    def _add_line(self, line):
        filenames, next = self.get_filenames_and_next()
        if filenames and os.path.getsize(filenames[-1]) < 10e6:
            filename = filenames[-1]
        else:
            filename = next
        
        with open(filename, 'ab') as f:
            f.write(line + '\n')
        
        return filename
    
    def add_share(self, share):
        for filename, (share_hashes, verified_hashes) in self.known.iteritems():
            if share.hash in share_hashes:
                break
        else:
            filename = self._add_line("%i %s" % (5, share_type.pack(share.as_share()).encode('hex')))
            share_hashes, verified_hashes = self.known.setdefault(filename, (set(), set()))
            share_hashes.add(share.hash)
        share_hashes, verified_hashes = self.known_desired.setdefault(filename, (set(), set()))
        share_hashes.add(share.hash)
    
    def add_verified_hash(self, share_hash):
        for filename, (share_hashes, verified_hashes) in self.known.iteritems():
            if share_hash in verified_hashes:
                break
        else:
            filename = self._add_line("%i %x" % (2, share_hash))
            share_hashes, verified_hashes = self.known.setdefault(filename, (set(), set()))
            verified_hashes.add(share_hash)
        share_hashes, verified_hashes = self.known_desired.setdefault(filename, (set(), set()))
        verified_hashes.add(share_hash)
    
    def get_filenames_and_next(self):
        suffixes = sorted(int(x[len(self.filename):]) for x in os.listdir(self.dirname) if x.startswith(self.filename) and x[len(self.filename):].isdigit())
        return [os.path.join(self.dirname, self.filename + str(suffix)) for suffix in suffixes], os.path.join(self.dirname, self.filename + (str(suffixes[-1] + 1) if suffixes else str(0)))
    
    def forget_share(self, share_hash):
        for filename, (share_hashes, verified_hashes) in self.known_desired.iteritems():
            if share_hash in share_hashes:
                share_hashes.remove(share_hash)
        self.check_remove()
    
    def forget_verified_share(self, share_hash):
        for filename, (share_hashes, verified_hashes) in self.known_desired.iteritems():
            if share_hash in verified_hashes:
                verified_hashes.remove(share_hash)
        self.check_remove()
    
    def check_remove(self):
        to_remove = set()
        for filename, (share_hashes, verified_hashes) in self.known_desired.iteritems():
            #print filename, len(share_hashes) + len(verified_hashes)
            if not share_hashes and not verified_hashes:
                to_remove.add(filename)
        for filename in to_remove:
            self.known.pop(filename)
            self.known_desired.pop(filename)
            os.remove(filename)
            print "REMOVED", filename
