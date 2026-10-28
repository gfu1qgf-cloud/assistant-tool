"""Bounded, read-only lookahead for same-image 10/12/15-point packs.

The caller still validates and allocates one account at a time. Future accounts
are predictions, never reservations. Binding batch budgets or heterogeneous
parameter permissions fall back to the existing constrained single-account DP.
"""
import math
import time

UNIT_POINTS = 100
MAX_EDGES = 20000
MAX_SECONDS = .025
COSTS = (10, 12, 15)


def capacity(credits):
    if type(credits) not in (int,float) or not math.isfinite(credits) or credits < 10:
        return 0
    return min(UNIT_POINTS,int(credits))


def improve(rows, clients, baseline, batches, observed, *, anchor, preferences, statistics=None):
    """Return a first pack only when a complete plan improves total utilization.

    Equal-cost interchangeable jobs are represented by three counts, not by
    credit-sized or task-subset state spaces. Search is memoized and bounded by
    both transitions and elapsed time; timeout keeps a complete feasible plan.
    """
    from .policy import estimated_cost
    from .routing import dispatch_rank
    stats=statistics if statistics is not None else {}
    stats.update(edges=0, improved=False, reason='single_account')
    if len(clients)<2 or not baseline:return baseline
    deadline=time.perf_counter()+MAX_SECONDS
    caps=tuple(capacity(c.get('credits')) for c in clients[:8])
    image=anchor['payload'].get('image_identity',{}).get('sha256')
    by_cost=[[],[],[]];batch_totals={};now=time.time()
    for row in rows:
        if row['status']!='queued' or row['remote'].get('credit_pack') or row['payload'].get('image_identity',{}).get('sha256')!=image:
            continue
        if row['remote'].get('prepare_retry',{}).get('next_at',0)>now or row['remote'].get('generation_retry'):
            continue
        cost=estimated_cost(row['payload'],observed)
        if type(cost) not in (int,float) or cost not in COSTS:
            stats['reason']='heterogeneous_cost';return baseline
        if row['payload'].get('budget') and cost>row['payload']['budget']:continue
        ranks=[dispatch_rank(c,row['payload'],preferences) for c in clients[:8]]
        if all(rank is None for rank in ranks):continue
        if any(rank is None for rank in ranks):
            stats['reason']='heterogeneous_parameters';return baseline
        by_cost[COSTS.index(cost)].append(row)
        group=row['payload'].get('batch_id','');batch_totals[group]=batch_totals.get(group,0)+cost
    for group,total in batch_totals.items():
        batch=batches.get(group,{})
        if batch.get('limit_total') and total>max(0,batch['limit_total']-batch.get('reserved',0)):
            stats['reason']='binding_batch_budget';return baseline
    counts=tuple(len(items) for items in by_cost)
    if not any(counts):stats['reason']='no_candidates';return baseline

    # Each account can take at most ten items. Pattern generation stays bounded
    # even if its real balance is thousands or the queue has thousands of jobs.
    patterns={}
    for cap in set(caps):
        options=[]
        for a in range(min(counts[0],cap//10)+1):
            for b in range(min(counts[1],(cap-a*10)//12)+1):
                for c in range(min(counts[2],(cap-a*10-b*12)//15)+1):
                    pattern=(a,b,c);spent=a*10+b*12+c*15
                    options.append((spent,pattern))
        # Diverse full packs are tried early; equal-quality outcomes retain the
        # old greedy pack below, so a tie alone never reorders submissions.
        options.sort(key=lambda p:(-p[0],-sum(v>0 for v in p[1]),p[1]))
        patterns[cap]=options

    def take(remaining,pattern):return tuple(a-b for a,b in zip(remaining,pattern))
    def fits(remaining,pattern):return all(a>=b for a,b in zip(remaining,pattern))
    def indices(remaining,pattern):
        picked=[]
        for kind,n in enumerate(pattern):
            start=counts[kind]-remaining[kind]
            picked.extend(positions[id(r)] for r in by_cost[kind][start:start+n])
        return tuple(sorted(picked))
    positions={id(row):i for i,row in enumerate(rows)}
    first_ids={row['id'] for row in baseline}
    first=tuple(sum(row['id'] in first_ids for row in group) for group in by_cost)
    if sum(first)!=len(baseline) or not fits(counts,first):
        stats['reason']='baseline_mismatch';return baseline
    remaining=take(counts,first);greedy=[first]
    for cap in caps[1:]:
        options=[(spent,p) for spent,p in patterns[cap] if fits(remaining,p)]
        maximum=options[0][0]
        pattern=min((p for spent,p in options if spent==maximum),key=lambda p:indices(remaining,p))
        greedy.append(pattern);remaining=take(remaining,pattern)
    total_points=sum(n*c for n,c in zip(counts,COSTS))
    best_score=total_points-sum(n*c for n,c in zip(remaining,COSTS));best_plan=tuple(greedy)
    initial=best_score;stats.update(reason='optimal_bound',baseline_points=initial,planned_points=initial)
    upper_cache={}

    def upper(index,remaining):
        key=index,remaining
        if key not in upper_cache:
            bits=1;max_cap=max(caps[index:],default=0)
            for count,cost in zip(remaining,COSTS):
                for _ in range(min(count,max_cap//cost)):bits|=bits<<cost
            available=sum(n*c for n,c in zip(remaining,COSTS))
            upper_cache[key]=min(available,sum((bits&((1<<(c+1))-1)).bit_length()-1 for c in caps[index:]))
        return upper_cache[key]

    if best_score>=upper(0,counts):return baseline
    visited=set();limited=False

    def search(index,remaining,score,plan):
        nonlocal best_score,best_plan,limited
        if index==len(caps):
            if score>best_score:best_score=score;best_plan=plan
            return
        if limited or score+upper(index,remaining)<=best_score:return
        key=index,remaining
        if key in visited:return
        visited.add(key)
        for spent,pattern in patterns[caps[index]]:
            stats['edges']+=1
            if stats['edges']>MAX_EDGES or (stats['edges']%32==0 and time.perf_counter()>=deadline):
                limited=True;return
            if index==0 and not spent or not fits(remaining,pattern):continue
            search(index+1,take(remaining,pattern),score+spent,plan+(pattern,))
            if limited or best_score>=score+upper(index,remaining):return

    if time.perf_counter()<deadline:search(0,counts,0,())
    else:limited=True
    stats.update(reason='bounded' if limited else 'searched',planned_points=best_score,improved=best_score>initial)
    if best_score<=initial:return baseline
    pattern=best_plan[0];chosen=[]
    for kind,n in enumerate(pattern):chosen.extend(by_cost[kind][:n])
    return sorted(chosen,key=lambda row:positions[id(row)])
