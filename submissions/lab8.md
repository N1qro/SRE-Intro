# Lab 8 - Chaos Engineering

## Setup

### Cluster and baseline

Started the local k3d cluster and used the Lab 7 in-cluster Prometheus:

```bash
k3d cluster start quickticket
export KUBECONFIG=/home/n1qro/.config/k3d/kubeconfig-quickticket.yaml
kubectl get nodes
```

Node state:

```text
NAME                       STATUS   ROLES                  VERSION
k3d-quickticket-server-0   Ready    control-plane,master   v1.31.5+k3s1
```

ArgoCD was still configured to self-heal from `feature/lab5`, so I paused automated reconciliation while collecting local Lab 8 evidence:

```bash
kubectl patch application quickticket -n argocd --type=merge -p '{"spec":{"syncPolicy":null}}'
```

The cluster PostgreSQL volume had restarted empty, so I reseeded it from the repo seed file before running load:

```bash
kubectl exec -i deployment/postgres -- psql -U quickticket -d quickticket -f /dev/stdin < app/seed.sql
```

Seed output:

```text
CREATE TABLE
CREATE TABLE
INSERT 0 5
```

I also increased live ticket counts to avoid the load generator producing artificial `409 Conflict` sellouts during sustained chaos runs:

```bash
kubectl exec deployment/postgres -- psql -U quickticket -d quickticket -c 'UPDATE events SET total_tickets = 100000;'
```

### Mixed load

The provided `labs/lab8/mixedload.yaml` could not pull `curlimages/curl` in my k3d cluster. I replaced only the live Deployment with an equivalent Python loop using the already available gateway image. The loop exercised the same flow: `GET /events`, `POST /events/1/reserve`, and `POST /reserve/{reservation_id}/pay`.

```text
deployment.apps/mixedload configured
deployment "mixedload" successfully rolled out
```

Baseline Prometheus query:

```text
$ sum(rate(gateway_requests_total[1m]))
13.723396975468974

$ sum(rate(gateway_requests_total{status=~"5.."}[1m])) / sum(rate(gateway_requests_total[1m]))
0
```

## Task 1 - Three Chaos Experiments

### Experiment 1 - Gateway pod kill under load

#### Hypothesis

If I delete one gateway pod while traffic is flowing, there should be no sustained user-visible failure because the Kubernetes Service routes traffic only to ready endpoints and the Argo Rollout restores the desired five replicas.

#### Method

```bash
victim=$(kubectl get pods -l app=gateway -o jsonpath="{.items[0].metadata.name}")
kubectl delete pod "$victim"
```

Run timestamp and victim:

```text
start=2026-06-28T14:49:49+03:00 victim=pod/gateway-76d48545b5-2swcf
pod "gateway-76d48545b5-2swcf" deleted from default namespace
```

#### Observations

The replacement pod existed almost immediately, but readiness waited for the gateway probe delay:

```text
t+3s ready=4/5 total=5 gateway-76d48545b5-bt85h:0/1:ContainerCreating
t+8s ready=4/5 total=5 gateway-76d48545b5-bt85h:0/1:Running
t+60s ready=4/5 total=5 gateway-76d48545b5-bt85h:0/1:Running
t+72s ready=5/5 total=5 gateway-76d48545b5-bt85h:1/1:Running
recovered_at=2026-06-28T14:51:01+03:00 elapsed=72s
```

Prometheus showed no gateway 5xx during the 3-minute window:

```text
$ sum(increase(gateway_requests_total{status=~"5.."}[3m]))
0
```

Traffic stayed on the remaining ready pods, then began reaching the replacement after it became ready:

```text
pod=gateway-76d48545b5-7tg9v  rps=3.109090909090909
pod=gateway-76d48545b5-6fs5g  rps=3.090909090909091
pod=gateway-76d48545b5-hvxct  rps=3.0907405050633607
pod=gateway-76d48545b5-6pkdz  rps=3.1999999999999997
pod=gateway-76d48545b5-bt85h  rps=1.43481
```

#### Comparison

The hypothesis was correct. Kubernetes created a replacement quickly and did not route Service traffic to it until it was ready. The only surprise was the difference between pod creation time and service-ready time: the pod was running by about 8 seconds, but full readiness took 72 seconds because of probe timing.

To improve resilience against this failure, I would keep multiple gateway replicas and tune readiness delays so replacement pods become eligible as soon as the app is truly ready.

### Experiment 2 - Payment latency injection

#### Hypothesis

If payments takes 2 seconds per request, the `/reserve/{id}/pay` path should become slower but the gateway should not return 5xx because 2000 ms is below `GATEWAY_TIMEOUT_MS=5000`.

#### Method

```bash
kubectl set env deployment/payments PAYMENT_LATENCY_MS=2000
kubectl rollout status deployment/payments --timeout=90s
```

Run timestamps:

```text
2026-06-28T14:52:13+03:00
deployment.apps/payments env updated
deployment "payments" successfully rolled out
2026-06-28T14:52:29+03:00
```

#### Observations at 2000 ms

After waiting for the Prometheus window:

```text
2026-06-28T14:54:25+03:00

$ sum(rate(gateway_requests_total{status=~"5.."}[1m])) / sum(rate(gateway_requests_total[1m]))
0
```

p99 latency by path:

```text
/health                 0.09316666666666662
/events                 0.07141666666666666
/events/{id}/reserve    0.21774999999999994
/reserve/{id}/pay       2.485
```

I then pushed latency beyond the timeout:

```bash
kubectl set env deployment/payments PAYMENT_LATENCY_MS=6000
kubectl rollout status deployment/payments --timeout=90s
```

Run timestamps:

```text
2026-06-28T14:54:58+03:00
deployment.apps/payments env updated
deployment "payments" successfully rolled out
2026-06-28T14:55:14+03:00
```

At 6000 ms:

```text
2026-06-28T14:57:07+03:00

$ sum(rate(gateway_requests_total{status=~"5.."}[1m])) / sum(rate(gateway_requests_total[1m]))
0.1538437877499888
```

p99 latency by path:

```text
/health                 0.09487505625843874
/events                 0.07237518751875187
/events/{id}/reserve    0.07412503333777837
/reserve/{id}/pay       7.475
```

Restored payments:

```bash
kubectl set env deployment/payments PAYMENT_LATENCY_MS=0 PAYMENT_FAILURE_RATE=0.0
kubectl rollout status deployment/payments --timeout=90s
```

Recovery timestamp:

```text
deployment "payments" successfully rolled out
2026-06-28T14:57:58+03:00
```

#### Comparison

The 2000 ms hypothesis was correct: payment latency increased only the payment path and did not create gateway 5xx. The 6000 ms test showed the gateway timeout boundary clearly: gateway 5xx ratio rose to about 15.4%, while read and reserve latency stayed low.

To improve resilience against this failure, I would alert on `/reserve/{id}/pay` p95 or p99 latency, not only on 5xx, because the 2000 ms case was user-visible degradation with no error-rate signal.

### Experiment 3 - Redis failure

#### Hypothesis

If Redis goes down, `/events` should still work because it reads from PostgreSQL, while reservation and payment flow should fail because reservations require Redis holds.

#### Method

```bash
kubectl scale deployment/redis --replicas=0
kubectl wait --for=delete pod -l app=redis --timeout=60s
```

Run timestamps:

```text
2026-06-28T14:59:40+03:00
deployment.apps/redis scaled
pod/redis-6d65768944-s4p8b condition met
2026-06-28T14:59:44+03:00
```

#### Observations

The in-cluster probe through the gateway Service saw connection failures:

```text
GET /events     URLError 0.031s <urlopen error [Errno 111] Connection refused>
POST /reserve   URLError 0.002s <urlopen error [Errno 111] Connection refused>
GET /health     URLError 0.002s <urlopen error [Errno 111] Connection refused>
```

Gateway error ratio during the failure:

```text
$ sum(rate(gateway_requests_total{status=~"5.."}[1m])) / sum(rate(gateway_requests_total[1m]))
0.42859379445266316
```

Endpoint and pod evidence explained the connection refused result:

```text
$ kubectl get endpoints gateway events redis -o wide
NAME      ENDPOINTS   AGE
gateway               8d
events                8d
redis     <none>      8d
```

```text
events-95d8c4bb5-t8nhg     0/1 Running
gateway-76d48545b5-6fs5g   0/1 Running
gateway-76d48545b5-6pkdz   0/1 Running
gateway-76d48545b5-7tg9v   0/1 Running
gateway-76d48545b5-bt85h   0/1 Running
gateway-76d48545b5-hvxct   0/1 Running
```

Events logs:

```text
Redis connection failed: Error 111 connecting to redis:6379. Connection refused.
```

Restored Redis:

```bash
kubectl scale deployment/redis --replicas=1
kubectl wait --for=condition=Available deployment/redis --timeout=90s
kubectl rollout status deployment/events --timeout=120s
kubectl argo rollouts status gateway --timeout=180s
```

Recovery timestamp:

```text
deployment "events" successfully rolled out
Healthy
2026-06-28T15:03:05+03:00
```

#### Comparison

The hypothesis was only partly correct. I expected reads through the gateway to continue while reservations failed. In reality, Redis failure made the events health endpoint fail, then gateway health failed, then gateway readiness removed every gateway pod from the Service. The whole public API became unreachable through `svc/gateway`.

To improve resilience against this failure, I would separate readiness from deep dependency health or make Redis non-critical for gateway readiness so read-only paths can continue when Redis is down.

## Task 2 - Combined Failure Scenario

### Scenario

I used a capacity crunch scenario:

```bash
kubectl scale deployment/mixedload --replicas=5
kubectl set env deployment/events DB_MAX_CONNS=2
kubectl rollout status deployment/events --timeout=120s
kubectl rollout status deployment/mixedload --timeout=120s
```

Run timestamps:

```text
2026-06-28T15:03:29+03:00
deployment.apps/mixedload scaled
deployment.apps/events env updated
deployment "events" successfully rolled out
deployment "mixedload" successfully rolled out
2026-06-28T15:04:41+03:00
```

Why this scenario: the workload exercises reads, reservations, payment, Redis, and PostgreSQL. Lowering `DB_MAX_CONNS` makes the single events pod a likely choke point.

### Observations

Sample 1:

```text
time=2026-06-28T15:05:11+03:00
error_ratio=0.02925561469094349

p99:
/events                 0.19609374999999957
/events/{id}/reserve    0.24455405405405403
/reserve/{id}/pay       0.4557352941176473
/health                 0.0970357142857143
```

Sample 2:

```text
time=2026-06-28T15:06:27+03:00
error_ratio=0.0656345663086285

p99:
/events                 0.07439880081474846
/events/{id}/reserve    0.07150080659738713
/reserve/{id}/pay       0.3862458321846851
/health                 0.09308279981657205
```

Sample 3:

```text
time=2026-06-28T15:07:43+03:00
error_ratio=0.06847388364573288

p99:
/events                 0.07624972179390156
/events/{id}/reserve    0.0739167345508853
/reserve/{id}/pay       0.42099623595301583
/health                 1.8699860893915652
```

Path-specific 5xx rate:

```text
/health status=503                 0.19999371961528936
/reserve/{id}/pay status=500       0.3818046296076859
/events status=502                 0.8908763666594043
/events/{id}/reserve status=502    0.018182479362885924
```

### Weakest link

The weakest link was the events service path under constrained database capacity. The clearest signal was `/events` returning 502 at the highest path-specific 5xx rate. `/reserve/{id}/pay` also showed elevated errors because payment confirmation depends on the reservation created by events.

The first golden signal to react was error rate. Latency did move, especially on `/pay` and `/health`, but the most actionable signal was the sustained gateway 5xx ratio rising to about 6-7%.

To make this more resilient, I would increase events serving capacity and avoid making a single events pod with a tiny DB pool responsible for all read and reservation traffic.

## Bonus Task - Resilience Improvement

### Weakness chosen

The combined scenario showed that one events pod with `DB_MAX_CONNS=2` produced sustained gateway errors under five mixedload replicas. I chose to improve events capacity by running two events replicas.

### Change

Changed `k8s/events.yaml`:

```diff
-  replicas: 1
+  replicas: 2
```

The image, Service selector, probes, resources, and environment defaults stayed unchanged.

### Before fix

Before the fix:

```text
events      READY 1/1
mixedload   READY 5/5
DB_MAX_CONNS=2
```

The capacity-crunch samples above showed:

```text
error_ratio sample 1: 0.02925561469094349
error_ratio sample 2: 0.0656345663086285
error_ratio sample 3: 0.06847388364573288
```

Worst path-specific error signal:

```text
/events status=502  0.8908763666594043
```

### After fix

Applied the two-replica events manifest and reran the same crunch with five mixedload replicas and `DB_MAX_CONNS=2`:

```text
events      READY 2/2
mixedload   READY 5/5
```

Events pods:

```text
events-7b46d67bb4-bqb6m   1/1 Running
events-7b46d67bb4-z5dfb   1/1 Running
```

After-fix sample 1:

```text
time=2026-06-28T15:17:04+03:00
error_ratio=0.04244694132334582

p99:
/events                 0.08869999999999995
/events/{id}/reserve    0.0734166666666666
/reserve/{id}/pay       0.44583333333333186
/health                 0.07949999999999978
```

After-fix sample 2:

```text
time=2026-06-28T15:18:20+03:00
error_ratio=0.046963189463681165

p99:
/events                 0.0793435889992276
/events/{id}/reserve    0.06951671256417277
/reserve/{id}/pay       0.3839263655984628
/health                 0.07950050905785365
```

After-fix sample 3:

```text
time=2026-06-28T15:19:36+03:00
error_ratio=0.030284633772428567

p99:
/events                 0.09487499886366729
/events/{id}/reserve    0.06522226857313698
/reserve/{id}/pay       0.2800000909066074
/health                 0.09703568886880273
```

Path-specific 5xx was lower after the fix:

```text
/events status=502              0.49090314071223856
/reserve/{id}/pay status=500    0.2727266115942891
/health status=503              0.10908958682494191
```

### Comparison and tradeoff

The two-replica fix reduced the sustained error ratio from about 6-7% before the fix to about 3-5% after the fix under the same load shape. It also reduced `/reserve/{id}/pay` p99 from about 0.42s in the final before-fix sample to about 0.28s in the final after-fix sample.

The tradeoff is higher baseline resource usage and potentially more database connections, but the Service has more events backends and a smaller single-pod blast radius.

## Cleanup

Removed mixedload, restored injected env vars, restored Redis, applied the final events manifest, restored ArgoCD automation, and stopped k3d:

```bash
kubectl delete -f labs/lab8/mixedload.yaml --ignore-not-found=true
kubectl set env deployment/payments PAYMENT_LATENCY_MS=0 PAYMENT_FAILURE_RATE=0.0
kubectl scale deployment/redis --replicas=1
kubectl apply -f k8s/events.yaml
kubectl patch application quickticket -n argocd --type=merge -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}'
k3d cluster stop quickticket
```

Final local events rollout before stopping:

```text
deployment "events" successfully rolled out

events-95d8c4bb5-8qc64   1/1 Running
events-95d8c4bb5-9hxf5   1/1 Running
```
