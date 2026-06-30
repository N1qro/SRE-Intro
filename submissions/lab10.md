# QuickTicket Reliability Review

## Setup

I ran Lab 10 from branch `feature/lab10` against the local k3d cluster:

```bash
k3d cluster start quickticket
export KUBECONFIG=/home/n1qro/.config/k3d/kubeconfig-quickticket.yaml
kubectl get nodes
```

Node output:

```text
NAME                       STATUS   ROLES                  AGE   VERSION
k3d-quickticket-server-0   Ready    control-plane,master   10d   v1.31.5+k3s1
```

ArgoCD was still configured to self-heal from `feature/lab5`, so I paused reconciliation while collecting local Lab 10 evidence and applied the current manifests from this branch:

```bash
kubectl patch application quickticket -n argocd --type=merge -p '{"spec":{"syncPolicy":null}}'
kubectl delete deployment gateway --ignore-not-found=true
kubectl apply -f k8s/redis.yaml -f k8s/postgres.yaml -f k8s/events.yaml \
  -f k8s/payments.yaml -f k8s/analysis-template.yaml -f k8s/gateway.yaml
```

Output:

```text
application.argoproj.io/quickticket patched
deployment.apps "gateway" deleted from default namespace
deployment.apps/redis configured
service/redis configured
deployment.apps/postgres configured
persistentvolumeclaim/postgres-data unchanged
service/postgres configured
deployment.apps/events configured
service/events configured
deployment.apps/payments configured
service/payments configured
analysistemplate.argoproj.io/gateway-error-rate unchanged
rollout.argoproj.io/gateway configured
service/gateway configured
```

The current gateway canary retried but its AnalysisRun failed during the warm cluster period. The stable ReplicaSet still served all traffic with five ready pods:

```bash
kubectl argo rollouts get rollout gateway
```

Relevant output:

```text
Status:          Degraded
Message:         RolloutAborted: Rollout aborted update to revision 15: Step-based analysis phase error/failed
Images:          ghcr.io/n1qro/quickticket-gateway:bd0118c82395970520aa51464877c757c330f704 (stable)
Replicas:
  Desired:       5
  Current:       5
  Updated:       0
  Ready:         5
  Available:     5
```

Gateway pod state after the load tests:

```text
NAME                       READY   STATUS    RESTARTS        AGE
gateway-5dc9568f86-2xlwg   1/1     Running   0               19m
gateway-5dc9568f86-4q2f9   1/1     Running   5 (29m ago)     3h56m
gateway-5dc9568f86-9hj6l   1/1     Running   5 (3m22s ago)   3h58m
gateway-5dc9568f86-mvlbs   1/1     Running   5 (3m15s ago)   3h56m
gateway-5dc9568f86-nj5ml   1/1     Running   4 (29m ago)     4h1m
```

I copied the provided Locust scenario to the repo root as `locustfile.py`, created the ConfigMap, flushed Redis, and increased event inventory so load tests measured service capacity instead of sold-out tickets:

```bash
kubectl create configmap locustfile \
  --from-file=locustfile.py=locustfile.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl exec -i $(kubectl get pod -l app=redis -o name) -- redis-cli FLUSHDB
kubectl exec $(kubectl get pod -l app=postgres -o name) -- \
  psql -U quickticket -d quickticket \
  -c 'UPDATE events SET total_tickets = 10000; SELECT id,total_tickets FROM events ORDER BY id;'
```

Output:

```text
configmap/locustfile created
OK
UPDATE 5
 id | total_tickets
----+---------------
  1 |         10000
  2 |         10000
  3 |         10000
  4 |         10000
  5 |         10000
(5 rows)
```

## 1. SLO Compliance

| SLO | Target | Observed | Status |
|---|---:|---:|---|
| Availability | 99.5% non-5xx over 7 days | 99.95% at 50 users, 99.69% at 100 users, 53.24% at 200 users | Pass until 100 users, fail at 200 users |
| Latency | 95% of gateway requests under 500 ms | p95 65 ms at 10 users, 120 ms at 50 users, 180 ms at 100 users, 1500 ms at 200 users | Pass until 100 users, fail at 200 users |

The practical capacity ceiling is between 100 and 200 Locust users. The first tested level that exceeded both thresholds was 200 users: aggregate p99 was 1900 ms and total failure rate was 46.76%.

## 2. Load Test Results

All Locust Jobs ran in the cluster against `http://gateway:8080`, so traffic went through the Kubernetes Service and was distributed across the five gateway pods. I flushed Redis between runs.

| Users | Ramp | RPS | p50 | p95 | p99 | 5xx/system failure rate | 409 inventory |
|------:|-----:|----:|----:|----:|----:|------------------------:|--------------:|
| 10 | 2/s | 7.50 | 28 ms | 65 ms | 89 ms | 0.00% | 0 |
| 50 | 5/s | 36.00 | 22 ms | 120 ms | 200 ms | 0.05% | 0 |
| 100 | 10/s | 71.42 | 24 ms | 180 ms | 350 ms | 0.31% | 0 |
| 200 | 20/s | 92.40 | 670 ms | 1500 ms | 1900 ms | 46.76% | 0 |

### 10 users

```bash
kubectl wait --for=condition=Complete job/load-10 --timeout=180s
kubectl logs job/load-10 | tail -40
```

Output:

```text
job.batch/load-10 condition met
Type     Name                         # reqs      # fails | Avg Min Max Med | req/s failures/s
GET      /events                         314     0(0.00%) |  32   8 365  27 |  5.28       0.00
POST     /events/3/reserve                70     0(0.00%) |  36  11 315  29 |  1.18       0.00
POST     /events/5/reserve                18     0(0.00%) |  36  12  71  35 |  0.30       0.00
GET      /health                          44     0(0.00%) |  37  15  92  34 |  0.74       0.00
Aggregated                               446     0(0.00%) |  34   8 365  28 |  7.50       0.00

Aggregated percentiles: 50%=28ms, 95%=65ms, 99%=89ms
```

### 50 users

Locust exited with code 1 because there was one 5xx, so I collected the Job logs directly:

```bash
kubectl logs job/load-50 | tail -80
```

Output:

```text
Type     Name                         # reqs      # fails | Avg Min Max Med | req/s failures/s
GET      /events                        1507     1(0.07%) |  32   5 253  20 | 25.30       0.02
POST     /events/3/reserve               329     0(0.00%) |  52   9 284  29 |  5.52       0.00
POST     /events/5/reserve               108     0(0.00%) |  54  10 268  24 |  1.81       0.00
GET      /health                         200     0(0.00%) |  40  10 188  28 |  3.36       0.00
Aggregated                              2144     1(0.05%) |  37   5 284  22 | 36.00       0.02

Aggregated percentiles: 50%=22ms, 95%=120ms, 99%=200ms

Error report:
1 GET /events: HTTPError('502 Server Error: Bad Gateway for url: /events')
```

### 100 users

```bash
kubectl logs job/load-100 | tail -100
```

Output:

```text
Type     Name                         # reqs      # fails | Avg Min Max Med | req/s failures/s
GET      /events                        2966    10(0.34%) |  46   7 509  22 | 49.76       0.17
POST     /events/3/reserve               624     3(0.48%) |  71   9 808  35 | 10.47       0.05
POST     /events/5/reserve               205     0(0.00%) |  73  10 503  36 |  3.44       0.00
GET      /health                         462     0(0.00%) |  50  10 433  25 |  7.75       0.00
Aggregated                              4257    13(0.31%) |  52   7 808  24 | 71.42       0.22

Aggregated percentiles: 50%=24ms, 95%=180ms, 99%=350ms

Error report:
2  POST /events/3/reserve: HTTPError('500 Server Error')
10 GET /events: HTTPError('502 Server Error')
1  POST /events/3/reserve: HTTPError('502 Server Error')
```

### 200 users - breaking point

```bash
kubectl logs job/load-200 | tail -120
```

Output:

```text
Type     Name                         # reqs       # fails | Avg Min  Max Med | req/s failures/s
GET      /events                        3890  1672(42.98%) | 693   3 2444 650 | 64.50      27.72
POST     /events/3/reserve               821   516(62.85%) | 811  12 2316 770 | 13.61       8.56
POST     /events/5/reserve               289   167(57.79%) | 814   4 2319 750 |  4.79       2.77
GET      /health                         573   251(43.80%) | 753   4 2705 710 |  9.50       4.16
Aggregated                              5573  2606(46.76%) | 723   3 2705 670 | 92.40      43.21

Aggregated percentiles: 50%=670ms, 95%=1500ms, 99%=1900ms

Error report:
149  POST /events/5/reserve: HTTPError('500 Server Error')
453  POST /events/3/reserve: HTTPError('500 Server Error')
1619 GET /events: HTTPError('502 Server Error')
246  GET /health: HTTPError('503 Server Error')
50   POST /events/3/reserve: HTTPError('502 Server Error')
17   POST /events/5/reserve: HTTPError('502 Server Error')
3    GET /events: RemoteDisconnected('Remote end closed connection without response')
69   ConnectionRefusedError(111, 'Connection refused')
```

At 200 users, both criteria were exceeded: p99 was above 500 ms and 5xx/system failures were far above 0.5%. I did not continue to 300 or 500 because the breaking point had already been found.

## 3. DORA Metrics

Source commands:

```bash
kubectl get rs -l app=gateway -o jsonpath='{.items[*].metadata.name}' | tr ' ' '\n' | wc -l
git log --oneline main | wc -l
kubectl get analysisrun -o jsonpath='{.items[*].status.phase}' | tr ' ' '\n' | sort | uniq -c
git log --oneline --decorate -20
```

Outputs:

```text
11
62
      1 Error
      5 Failed
      3 Successful
```

Recent Git history:

```text
aa3fe94 ci: update image tags to 948b53213d03bf35f8134fce18d5b99393bf3374
948b532 Merge pull request #10 from N1qro/feature/lab9
a0e6313 feat(lab9): add migrations and database reliability report
e3768db ci: update image tags to bd0118c82395970520aa51464877c757c330f704
bd0118c Merge pull request #9 from N1qro/feature/lab8
e8bab4d feat(lab8): add chaos engineering report
16d36f5 ci: update image tags to f5eeaf12b8cbd769de1398f7d48d960f339f8b2a
f5eeaf1 Merge pull request #7 from N1qro/feature/lab7
0086c8b ci: update image tags to 2544c7d7864235734f81510fbd7bc1fc5163890b
2544c7d Merge pull request #8 from N1qro/fix/ci-push-race
7e8e6bb feat(lab7): add gateway canary rollout
0115b03 fix(ci): skip stale manifest updates
69e2df5 ci: update image tags to 644047234546d9f31e85b566a99f9c1653df86f4
6440472 Merge pull request #6 from N1qro/feature/lab6
af08af1 Merge pull request #5 from N1qro/feature/lab5
e44b340 feat(lab6): add alerting config, runbook, and postmortem
de51213 ci: update image tags to 5d126efa622bb8d852c6021d0777ae55d9d48a4c
5d126ef docs(lab5): complete CI/CD and GitOps report
af94e3b revert: rollback broken gateway image [skip ci]
7fb3804 feat: deploy broken gateway image [skip ci]
```

| Metric | Value | Interpretation |
|---|---:|---|
| Deployment frequency | 11 gateway ReplicaSets across the course cluster; 62 commits on `main` | Frequent small lab deployments, roughly one deployable change per lab plus CI image-tag updates |
| Lead time for changes | Minutes to under 1 day | In this course setup: PR merge, CI image build, manifest update, then ArgoCD polling/sync |
| Change failure rate | 6 unsuccessful AnalysisRuns out of 9 observed, or 66.7% in the training cluster | Inflated by deliberate bad canaries and warm-cluster analysis tests; production target should be 0-15% |
| Recovery time | Argo Rollouts abort stopped bad canary exposure in about 3 seconds; Git revert rollback took about 2-3 minutes | Progressive delivery recovered faster than GitOps-only rollback |
| Data recovery time | Lab 9 no-PVC RTO 340s; PVC-backed practical RTO 155s | Stateful recovery remains slower than stateless rollback |

## 4. Top 3 Reliability Risks

1. **Events/Postgres path overload.** Lab 8 capacity crunch and Lab 10 load testing both point at the read/reserve path as the weak link. At 200 users, `/events` returned many 502s and reserve returned 500/502 errors. Fix: scale events, tune DB pools, add Postgres connection pooling, and test higher DB capacity.
2. **Canary analysis is sensitive to noisy warm-up periods.** The gateway Rollout aborted during Lab 10 setup because the analysis measured errors while the cluster was settling. Fix: add traffic-volume guards, latency checks, and analysis windows that distinguish startup noise from real canary failure.
3. **Stateful recovery still depends on manual procedures.** Lab 9 improved Postgres with a PVC and backup CronJob, but restore validation and RPO/RTO tracking are still manual. Fix: alert on backup freshness, run scheduled restore drills, and add WAL/PITR for smaller RPO.

## 5. Toil Identification

| Toil item | Frequency observed | Automation | Saved effort |
|---|---:|---|---|
| Reseeding Postgres and raising ticket counts after restarts | More than 3 times across Labs 4, 8, 9, and 10 | Add an idempotent Kubernetes Job or Make target for seed/reset data | Avoids repeated `kubectl exec ... psql` commands and inconsistent test inventory |
| Recreating temporary load/probe workloads | More than 3 times across chaos, migration, and load labs | Keep reusable Job templates with parameters or a small `make load USERS=...` wrapper | Reduces command mistakes and makes reports easier to reproduce |
| Manually watching rollouts/alerts and copying evidence | More than 3 times in Labs 6, 7, 8, and 10 | Add scripted evidence collectors for rollout state, AnalysisRuns, Prometheus queries, and pod CPU | Saves time and reduces missing-proof grading risk |

## 6. Monitoring Gaps

- I needed a latency alert, not only an error-rate alert. Lab 8 showed slow payments at 2000 ms with no 5xx, and Lab 10 showed p99 exploding at 200 users.
- I needed dependency-specific error panels. Gateway 502s did not immediately tell whether events, Postgres, Redis, or gateway workers were the bottleneck.
- I needed backup freshness and restore-success monitoring. Lab 9 proved that having a dump is not enough; the restore path has to be measured.
- I needed canary traffic-volume checks. A canary analysis should not pass or fail confidently without enough traffic and a quiet startup baseline.

An alert that would have caught the Lab 10 breakage:

```text
gateway p99 latency > 500ms for 2 minutes OR gateway 5xx ratio > 0.5% for 2 minutes,
grouped by path, with a runbook link to inspect events/Postgres saturation.
```

## 7. Capacity Plan

Breaking point: `200` Locust users, `92.40` RPS, `46.76%` failures, aggregate p99 `1900 ms`.

CPU at the breaking-point window:

```bash
kubectl top pods -l app=gateway
kubectl top pods -l app=events
kubectl top pods -l app=payments
kubectl top pods -l app=postgres
kubectl top pods -l app=redis
```

Output:

```text
NAME                       CPU(cores)   MEMORY(bytes)
gateway-5dc9568f86-2xlwg   13m          47Mi
gateway-5dc9568f86-4q2f9   11m          48Mi
gateway-5dc9568f86-9hj6l   11m          46Mi
gateway-5dc9568f86-mvlbs   7m           38Mi
gateway-5dc9568f86-nj5ml   14m          50Mi

NAME                     CPU(cores)   MEMORY(bytes)
events-94b6cb7b5-dz8km   6m           58Mi
events-94b6cb7b5-fgsqq   10m          58Mi

NAME                        CPU(cores)   MEMORY(bytes)
payments-644674cbb4-5g2mw   10m          36Mi

NAME                        CPU(cores)   MEMORY(bytes)
postgres-6fc5585b5b-xbksd   3m           41Mi

NAME                     CPU(cores)   MEMORY(bytes)
redis-6d65768944-24z7k   19m          8Mi
```

The CPU snapshot was not saturated, so I would not solve the 200-user failure by blindly raising CPU limits. The symptoms look more like service readiness/restarts, request queueing, and events/Postgres connection-path limits.

For 2x safe traffic over the healthy 100-user level, I would plan for about `140-150 RPS` without crossing 500 ms p99:

| Component | Current | 2x plan | Resource request/limit |
|---|---:|---:|---|
| Gateway | 5 replicas | 8 replicas | keep 50m/64Mi request, 200m/256Mi limit until CPU proves otherwise |
| Events | 2 replicas | 4 replicas | 75m/96Mi request, 300m/384Mi limit; keep `DB_MAX_CONNS=10` initially |
| Payments | 1 replica | 2 replicas | 50m/64Mi request, 200m/256Mi limit |
| Redis | 1 replica | 1 primary plus backup/replica for real production | CPU was low, but single-pod Redis is an availability risk |
| Postgres | 1 PVC-backed pod | keep one for lab; add connection pooler and PITR for production | raise memory before increasing app DB clients |

Rough small-cloud cost with the lab hint of `$5/pod/month`:

```text
Current app/data pods: gateway 5 + events 2 + payments 1 + postgres 1 + redis 1 = 10 pods ~= $50/month
2x plan: gateway 8 + events 4 + payments 2 + postgres 1 + redis 1 = 16 pods ~= $80/month
Incremental cost: about $30/month
```

Before adopting the plan, I would rerun the 100/150/200-user Locust sequence with the scaled events/gateway plan and verify p99 stays under 500 ms and 5xx remains under 0.5%.
