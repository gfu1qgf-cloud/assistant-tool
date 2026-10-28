"""Bounded pack planning: exact small oracles, safety filters, no real browser."""
import copy
import itertools
import random
import unittest
from unittest.mock import patch

from app_plugins.builtin.flow_assistant import credit_packs as packs,pack_planner as planner
import test_flow_credit_packs as fixtures

COST={6:10,8:12,10:15}


def rows_for(durations):
    return [{'id':str(i),'status':'queued','remote':{},'payload':{'model':'omni_1_1_flash','duration':d,
        'image_identity':{'sha256':'a'*64},'batch_id':'A','budget':0}} for i,d in enumerate(durations)]


def clients_for(balances):return [{'id':str(i),'profile':str(i),'credits':balance} for i,balance in enumerate(balances)]


def planned(rows,balances,**kwargs):
    clients=clients_for(balances);stats={}
    baseline=packs.optimize(rows,balances[0],{}, {},anchor=rows[0],client=clients[0],preferences={})
    choice=planner.improve(rows,clients,baseline,{}, {},anchor=rows[0],preferences={},statistics=stats,**kwargs)
    return choice,stats


class PlannerTests(unittest.TestCase):
    def test_fifty_one_and_large_balances_use_real_tail_then_hundred_point_units(self):
        for balance,expected in ((51,51),(99,99),(100,100),(1000,100),(10**12,100)):
            rows=rows_for([10]*10+[8]*10+[6]*10)
            selected=packs.optimize(rows,balance,{}, {},anchor=rows[0],client=clients_for([balance])[0],preferences={})
            self.assertEqual(sum(COST[r['payload']['duration']] for r in selected),expected)
            self.assertLessEqual(len(selected),10)
        for bad in (None,True,float('nan'),float('inf'),-1,9):self.assertEqual(planner.capacity(bad),0)

    def test_three_accounts_use_fifty_each_without_a_stranded_six_second_job(self):
        rows=rows_for([6]*6+[10]*6);remaining=rows;totals=[]
        for n in (3,2,1):
            selected,stats=planned(remaining,[50]*n)
            totals.append(sum(COST[r['payload']['duration']] for r in selected))
            used={r['id'] for r in selected};remaining=[r for r in remaining if r['id'] not in used]
        self.assertEqual(totals,[50,50,50]);self.assertFalse(remaining)

    def test_small_joint_plans_match_exhaustive_assignment_oracle(self):
        randomizer=random.Random(261026)
        for _ in range(110):
            rows=rows_for([randomizer.choice((6,8,10)) for _ in range(randomizer.randint(2,7))])
            balances=[randomizer.choice((12,15,25,30,37,50,51,1000)) for _ in range(randomizer.choice((2,3)))]
            clients=clients_for(balances)
            baseline=packs.optimize(rows,balances[0],{}, {},anchor=rows[0],client=clients[0],preferences={})
            if not baseline:continue
            best=0
            for assignment in itertools.product(range(len(balances)+1),repeat=len(rows)):
                spends=[sum(COST[r['payload']['duration']] for r,k in zip(rows,assignment) if k==i) for i in range(len(balances))]
                if spends[0] and all(total<=planner.capacity(balance) for total,balance in zip(spends,balances)):best=max(best,sum(spends))
            stats={}
            planner.improve(rows,clients,baseline,{}, {},anchor=rows[0],preferences={},statistics=stats)
            self.assertEqual(stats['planned_points'],best,(balances,[r['payload']['duration'] for r in rows],stats))

    def test_timeout_and_transition_limit_keep_a_complete_valid_baseline(self):
        rows=rows_for([6]*6+[10]*6);clients=clients_for([50]*3)
        baseline=packs.optimize(rows,50,{}, {},anchor=rows[0],client=clients[0],preferences={})
        for limits in ({'MAX_EDGES':0},{'MAX_SECONDS':0}):
            with patch.multiple(planner,**limits):
                stats={};selected=planner.improve(rows,clients,baseline,{}, {},anchor=rows[0],preferences={},statistics=stats)
            self.assertEqual([r['id'] for r in selected],[r['id'] for r in baseline]);self.assertEqual(stats['reason'],'bounded')

    def test_binding_batch_budget_and_different_lane_permissions_use_constrained_dp(self):
        rows=rows_for([6]*6+[10]*6);clients=clients_for([50]*3)
        for batches,prefs,reason in (({'A':{'limit_total':50,'reserved':10}},{},'binding_batch_budget'),
                ({},{'auto_lane':False,'profile_lanes':{'1':10}},'heterogeneous_parameters')):
            baseline=packs.optimize(rows,50,batches,{},anchor=rows[0],client=clients[0],preferences=prefs);stats={}
            selected=planner.improve(rows,clients,baseline,batches,{},anchor=rows[0],preferences=prefs,statistics=stats)
            self.assertEqual(selected,baseline);self.assertEqual(stats['reason'],reason)

    def test_other_images_retry_holds_and_unreleased_rows_are_never_added(self):
        rows=rows_for([6]*6+[10]*6)
        forbidden=[]
        for updates in ({'status':'collected'},{'remote':{'generation_retry':{'state':'waiting'}}},
                {'remote':{'prepare_retry':{'next_at':10**12}}},{'remote':{'credit_pack':{'id':'old'}}}):
            item=copy.deepcopy(rows[0]);item['id']='blocked-'+str(len(forbidden));item.update(updates);forbidden.append(item)
        other=copy.deepcopy(rows[0]);other['id']='other-image';other['payload']['image_identity']['sha256']='b'*64
        original=copy.deepcopy(rows+forbidden+[other]);selected,_=planned(rows+forbidden+[other],[50]*3)
        self.assertTrue(all(r['id'].isdigit() for r in selected));self.assertEqual(rows+forbidden+[other],original)

    def test_single_account_dp_still_matches_budgeted_bruteforce_after_pruning(self):
        randomizer=random.Random(126)
        for _ in range(160):
            rows=rows_for([randomizer.choice((6,8,10)) for _ in range(randomizer.randint(1,9))]);balance=randomizer.choice((22,48,50,51,99,1000))
            batches={g:{'limit_total':randomizer.choice((0,24,50,100)),'reserved':randomizer.choice((0,10))} for g in ('A','B')}
            for row in rows:row['payload']['batch_id']=randomizer.choice(('A','B'))
            candidates=[]
            for mask in range(1<<len(rows)):
                indices=tuple(i for i in range(len(rows)) if mask>>i&1);spent=sum(COST[rows[i]['payload']['duration']] for i in indices)
                if spent>planner.capacity(balance):continue
                if any(b['limit_total'] and sum(COST[rows[i]['payload']['duration']] for i in indices if rows[i]['payload']['batch_id']==g)>max(0,b['limit_total']-b['reserved']) for g,b in batches.items()):continue
                candidates.append((spent,indices))
            expected=min(candidates,key=lambda pair:(-pair[0],pair[1]))
            result=packs.optimize(rows,balance,batches,{},anchor=rows[0],client=clients_for([balance])[0],preferences={})
            self.assertEqual(tuple(int(row['id']) for row in result),expected[1])


class DispatchTests(unittest.TestCase):
    setUpClass=fixtures.PackTests.__dict__['setUpClass']
    setUp=fixtures.PackTests.setUp
    setup_pack=fixtures.PackTests.setup_pack
    service=fixtures.PackTests.service
    jobs=fixtures.PackTests.jobs
    client=fixtures.PackTests.client
    dispatch=fixtures.PackTests.dispatch

    def ready(self,service,profile,credits):
        client=self.client(service,profile,credits=credits)
        client.update(capabilities=['credit_pack_submit_v1','ui_mutex_v1'],credit_pack_submit=True,
            page_ready=True,credits_local=True)
        service.preferences['profile_projects'][profile]=client['saved_project']
        return client

    def test_dispatch_looks_ahead_but_allocates_only_one_account_and_never_charges(self):
        service,first=self.setup_pack([6]*6+[10]*6)
        second=self.ready(service,'Profile 9',50);third=self.ready(service,'Profile 10',50);service.preferences['concurrency']=3
        self.assertTrue(self.dispatch(service))
        rows=service.store.rows();allocated=[r for r in rows if r['remote'].get('credit_pack')]
        self.assertEqual(sorted(r['payload']['duration'] for r in allocated),[6,6,10,10])
        self.assertEqual({r['remote']['credit_pack']['browser'] for r in allocated},{first['id']})
        self.assertEqual(service.store.batches()[rows[0]['payload']['batch_id']]['reserved'],0)
        self.assertFalse(any(p['client'] in {second['id'],third['id']} for p in service.pending.values()))

    def test_busy_duplicate_stale_and_unapproved_high_accounts_do_not_inflate_lookahead(self):
        service,first=self.setup_pack([6]*6+[10]*6);service.preferences['concurrency']=8
        busy=self.ready(service,'Profile 9',50);busy['native_busy']=True
        duplicate=self.ready(service,'Profile 10',50);duplicate['account']=first['account']
        stale=self.ready(service,'Profile 11',50);stale['credits_at']=1
        high=self.ready(service,'Profile 12',1000)
        peers=[]
        from app_plugins.builtin.flow_assistant.image_affinity import affinity_context
        rows=service.store.rows();affinity=affinity_context(list(service.clients.values()),rows,service.preferences,{r['id'] for r in rows})
        peers=packs.planning_clients(service,first,list(service.clients.values()),rows,rows,8,affinity,[])
        self.assertEqual(peers,[first]);self.assertTrue(self.dispatch(service))
        self.assertEqual(len([r for r in service.store.rows() if r['remote'].get('credit_pack')]),5)

    def test_allowed_thousand_point_account_gets_at_most_hundred_and_unapproved_gets_none(self):
        service,client=self.setup_pack([6]*10,balance=1000)
        service.store.enqueue(self.jobs(count=12,duration=6,budget=0))
        self.assertFalse(self.dispatch(service));self.assertFalse(service.pending)
        service.preferences['high_credit_accounts']={client['account']:{'permission':'allow'}}
        self.assertTrue(self.dispatch(service))
        rows=service.store.rows();allocated=[r for r in rows if r['remote'].get('credit_pack')]
        self.assertEqual(len(allocated),10);self.assertEqual(allocated[0]['remote']['credit_pack']['total'],100)
        self.assertEqual(client['credits'],1000,'planning never debits ahead of a real send')

    def test_new_planning_never_rewrites_existing_allocations(self):
        service,first=self.setup_pack([6]*6+[10]*6);service.preferences['concurrency']=3
        self.ready(service,'Profile 9',50);self.ready(service,'Profile 10',50);self.dispatch(service)
        frozen={r['id']:copy.deepcopy(r['remote']['credit_pack']) for r in service.store.rows() if r['remote'].get('credit_pack')}
        self.dispatch(service)
        self.assertEqual({r['id']:r['remote']['credit_pack'] for r in service.store.rows() if r['id'] in frozen},frozen)


if __name__=='__main__':unittest.main()
