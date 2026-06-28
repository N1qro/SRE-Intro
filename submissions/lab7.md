# Lab 7 - Progressive Delivery: Canary Deployments

## Task 1 - Manual Canary Deployment

### Argo Rollouts installation

Installed the Argo Rollouts controller in the `argo-rollouts` namespace:

```bash
kubectl create namespace argo-rollouts
kubectl apply -n argo-rollouts -f https://github.com/argoproj/argo-rollouts/releases/latest/download/install.yaml
kubectl wait --for=condition=Available deployment/argo-rollouts -n argo-rollouts --timeout=120s
```

The direct GitHub binary download for the kubectl plugin timed out from my environment, so I extracted the same `v1.9.0` plugin binary from the official Quay image:

```text
quay.io/argoproj/kubectl-argo-rollouts:v1.9.0
```

Version output:

```text
$ kubectl argo rollouts version
kubectl-argo-rollouts: v1.9.0+838d4e7
  BuildDate: 2026-03-20T21:11:23Z
  GitCommit: 838d4e792be666ec11bd0c80331e0c5511b5010e
  GitTreeState: clean
  GoVersion: go1.24.13
  Compiler: gc
  Platform: linux/amd64
```

### Gateway converted to Rollout

Converted `k8s/gateway.yaml` from a Deployment to an Argo Rollout:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Rollout
metadata:
  name: gateway
spec:
  replicas: 5
  strategy:
    canary:
      steps:
        - setWeight: 20
        - pause: {}
        - setWeight: 60
        - pause:
            duration: 30s
        - setWeight: 100
```

Deleted the old Deployment and applied the Rollout:

```bash
kubectl delete deployment gateway --ignore-not-found=true
kubectl apply -f k8s/gateway.yaml
```

Stable baseline:

```text
Name:            gateway
Status:          Healthy
Strategy:        Canary
  Step:          5/5
  SetWeight:     100
  ActualWeight:  100
Replicas:
  Desired:       5
  Current:       5
  Updated:       5
  Ready:         5
  Available:     5
```

### Manual canary paused at 20%

Triggered a good canary by changing `APP_VERSION` to `v7-manual-good`.

Rollout output:

```text
Name:            gateway
Status:          Paused
Message:         CanaryPauseStep
Strategy:        Canary
  Step:          1/5
  SetWeight:     20
  ActualWeight:  20
Replicas:
  Desired:       5
  Current:       5
  Updated:       1
  Ready:         5
  Available:     5

revision:2  ReplicaSet gateway-85c6f4ff55  canary
  gateway-85c6f4ff55-jr7ww  ready:1/1
revision:1  ReplicaSet gateway-98b7468fb   stable
  four stable pods ready:1/1
```

Pod split:

```text
gateway-85c6f4ff55-jr7ww  app_version=v7-manual-good
gateway-98b7468fb-9rp82   app_version=v7-stable
gateway-98b7468fb-pjnbq   app_version=v7-stable
gateway-98b7468fb-snzng   app_version=v7-stable
gateway-98b7468fb-wl27c   app_version=v7-stable
```

### Traffic split proof

The provided `labs/lab7/loadgen.yaml` could not pull `curlimages/curl` in my k3d cluster because Docker Hub pulls timed out. I used an equivalent temporary in-cluster pod based on the already-present gateway image:

```bash
kubectl run lab7-loadgen \
  --image=ghcr.io/n1qro/quickticket-gateway:644047234546d9f31e85b566a99f9c1653df86f4 \
  --image-pull-policy=IfNotPresent \
  --restart=Never \
  --command -- python -u -c '...requests to http://gateway:8080/events and /health...'
```

This still tests the required path: traffic originates inside the cluster and goes through the `gateway` Service/kube-proxy, not a sticky `kubectl port-forward`.

Per-pod `/events` counts over the sample window:

```text
pod/gateway-85c6f4ff55-jr7ww app_version=v7-manual-good events_requests=26
pod/gateway-98b7468fb-9rp82  app_version=v7-stable      events_requests=33
pod/gateway-98b7468fb-pjnbq  app_version=v7-stable      events_requests=36
pod/gateway-98b7468fb-snzng  app_version=v7-stable      events_requests=35
pod/gateway-98b7468fb-wl27c  app_version=v7-stable      events_requests=41
```

The canary pod received real service traffic while paused at 20%. The short sample has variance, but requests were distributed across stable and canary pods through the Service.

### Manual promotion

Promoted the canary:

```bash
kubectl argo rollouts promote gateway
```

During promotion to 60%:

```text
Status:          Progressing
Strategy:        Canary
  Step:          2/5
  SetWeight:     60
  ActualWeight:  25
Replicas:
  Desired:       5
  Current:       6
  Updated:       3
  Ready:         4
  Available:     4
```

Final promoted state:

```text
Name:            gateway
Status:          Healthy
Strategy:        Canary
  Step:          5/5
  SetWeight:     100
  ActualWeight:  100
Replicas:
  Desired:       5
  Current:       5
  Updated:       5
  Ready:         5
  Available:     5
```

### Manual abort

Triggered a new canary marked `APP_VERSION=v7-manual-bad` and waited for the 20% pause:

```text
Status:          Paused
Message:         CanaryPauseStep
Strategy:        Canary
  Step:          1/5
  SetWeight:     20
  ActualWeight:  20
Replicas:
  Desired:       5
  Current:       5
  Updated:       1
  Ready:         5
  Available:     5
```

Abort command and timestamps:

```text
2026-06-28T12:43:01+03:00
$ kubectl argo rollouts abort gateway
rollout 'gateway' aborted
2026-06-28T12:43:04+03:00
```

After abort:

```text
Status:          Degraded
Message:         RolloutAborted: Rollout aborted update to revision 3
Strategy:        Canary
  Step:          0/5
  SetWeight:     0
  ActualWeight:  0
Images:          ghcr.io/n1qro/quickticket-gateway:644047234546d9f31e85b566a99f9c1653df86f4 (stable)
```

All stable pods were serving again after the canary was scaled down:

```text
Replicas:
  Desired:       5
  Current:       5
  Updated:       0
  Ready:         5
  Available:     5
```

Abort to stable-serving traffic took about 3 seconds for the command to return and scale the canary down. A replacement stable pod then waited through the gateway readiness delay. Compared with Lab 5's Git revert rollback, which took about 2-3 minutes from pushing the revert to observing healthy pods, Argo Rollouts abort was much faster for stopping bad canary exposure.

## Task 2 - Multi-Step Canary with Observation

### Strategy used

Applied a more granular canary strategy:

```yaml
strategy:
  canary:
    steps:
      - setWeight: 20
      - pause:
          duration: 60s
      - setWeight: 40
      - pause:
          duration: 60s
      - setWeight: 60
      - pause:
          duration: 60s
      - setWeight: 80
      - pause:
          duration: 30s
      - setWeight: 100
```

Triggered the rollout with `APP_VERSION=v7-multistep`.

### Observation snapshots

20% step:

```text
Status:          Paused
Strategy:        Canary
  Step:          1/9
  SetWeight:     20
  ActualWeight:  20
Replicas:
  Desired:       5
  Current:       5
  Updated:       1
  Ready:         5
  Available:     5
```

60% step:

```text
Status:          Progressing
Strategy:        Canary
  Step:          4/9
  SetWeight:     60
  ActualWeight:  50
Replicas:
  Desired:       5
  Current:       5
  Updated:       3
  Ready:         4
  Available:     4
```

80% step:

```text
Status:          Progressing
Strategy:        Canary
  Step:          6/9
  SetWeight:     80
  ActualWeight:  75
Replicas:
  Desired:       5
  Current:       5
  Updated:       4
  Ready:         4
  Available:     4
```

Final state:

```text
Status:          Healthy
Strategy:        Canary
  Step:          9/9
  SetWeight:     100
  ActualWeight:  100
Replicas:
  Desired:       5
  Current:       5
  Updated:       5
  Ready:         5
  Available:     5
```

### Dashboard and traffic observation

The docker-compose Grafana/Prometheus stack from Lab 3 cannot directly scrape pod IPs inside k3d. For this task I used:

- `kubectl argo rollouts get rollout gateway` for canary step, weight, and replica observation
- in-cluster loadgen traffic through `svc/gateway`
- per-pod gateway logs to confirm requests were hitting both stable and canary pods

Request rate stayed steady from the loadgen side while Argo Rollouts changed the updated replica count from 1 to 3 to 4 to 5.

### Automated abort threshold

I would want automated abort at 20% if canary 5xx error rate is above 5% for more than one or two consecutive measurement windows. At 20%, the new version has real user traffic but limited blast radius. Waiting until 60% or 80% would expose too many users to a clearly bad release.

## Bonus Task - Automated Canary Analysis

### In-cluster Prometheus

Applied the provided in-cluster Prometheus:

```bash
kubectl apply -f labs/lab7/prometheus.yaml
kubectl -n monitoring rollout status deployment/prometheus --timeout=120s
```

The cluster could not pull `prom/prometheus:v3.11.2` from Docker Hub, so I imported the already-local image into k3d:

```bash
k3d image import prom/prometheus:v3.11.2 -c quickticket
kubectl -n monitoring rollout restart deployment/prometheus
```

Prometheus discovered all gateway pods with `rs_hash`:

```text
gateway-554f45cffb-5k5dx rs=554f45cffb up
gateway-554f45cffb-678gq rs=554f45cffb up
gateway-554f45cffb-m7bt5 rs=554f45cffb up
gateway-554f45cffb-sgmzf rs=554f45cffb up
gateway-554f45cffb-rkhlh rs=554f45cffb up
```

### AnalysisTemplate

Applied `k8s/analysis-template.yaml`:

```text
$ kubectl get analysistemplate gateway-error-rate
NAME                 AGE
gateway-error-rate   36m
```

The template queries only the current canary hash:

```promql
(
  sum(rate(gateway_requests_total{rs_hash="{{args.canary-hash}}",status=~"5.."}[60s]))
  or on() vector(0)
)
/
sum(rate(gateway_requests_total{rs_hash="{{args.canary-hash}}"}[60s]))
```

### Good version auto-promotes

Final analysis-backed strategy:

```yaml
strategy:
  canary:
    steps:
      - setWeight: 20
      - pause:
          duration: 20s
      - analysis:
          templates:
            - templateName: gateway-error-rate
          args:
            - name: canary-hash
              valueFrom:
                podTemplateHashValue: Latest
      - setWeight: 50
      - pause:
          duration: 20s
      - setWeight: 100
```

Successful AnalysisRun:

```text
$ kubectl get analysisrun
NAME                     STATUS       AGE
gateway-5d4bc8f776-6-2   Successful   18m
gateway-65d9778575-5-2   Failed       25m
```

Successful measurement values:

```text
name: gateway-5d4bc8f776-6-2
phase: Successful
metric: error-rate
measurements:
  - value: [0]
  - value: [0]
  - value: [0]
successful: 3
```

After the successful analysis, the rollout continued and reached Healthy:

```text
Status:          Healthy
Strategy:        Canary
  Step:          6/6
  SetWeight:     100
  ActualWeight:  100
Replicas:
  Desired:       5
  Current:       5
  Updated:       5
  Ready:         5
  Available:     5
```

### Bad version auto-aborts

To produce a measurable bad canary without committing a broken manifest, I patched the live Rollout template only:

```text
EVENTS_URL=http://broken-on-purpose:8081
GATEWAY_TIMEOUT_MS=2000
APP_VERSION=v7-broken-events-ready
```

The gateway `/health` endpoint checks the events dependency, so a broken `EVENTS_URL` makes the canary fail readiness before it can receive traffic. For this live-only proof, I pointed the canary probes at `/metrics`; then the pod stayed Ready, `/events` returned 502, and Prometheus could measure the canary error rate.

AnalysisRun list:

```text
$ kubectl get analysisrun
NAME                      STATUS       AGE
gateway-5d4bc8f776-6-2    Successful   33m
gateway-68bb686c74-11-2   Failed       119s
```

Failed AnalysisRun details:

```text
name: gateway-68bb686c74-11-2
phase: Failed
message: Metric "error-rate" assessed Failed due to failed (2) > failureLimit (1)
args:
  canary-hash: 68bb686c74
measurements:
  - value: [1]
    phase: Failed
  - value: [1]
    phase: Failed
```

The resolved Prometheus query was scoped to the canary hash:

```promql
(
  sum(rate(gateway_requests_total{rs_hash="68bb686c74",status=~"5.."}[60s]))
  or on() vector(0)
)
/
sum(rate(gateway_requests_total{rs_hash="68bb686c74"}[60s]))
```

Rollout result:

```text
Status:          Degraded
Message:         RolloutAborted: Rollout aborted update to revision 11:
                 Step-based analysis phase error/failed:
                 Metric "error-rate" assessed Failed due to failed (2) > failureLimit (1)
Strategy:        Canary
  Step:          0/6
  SetWeight:     0
  ActualWeight:  0
Replicas:
  Desired:       5
  Current:       5
  Updated:       0
```

Argo Rollouts scaled the bad canary down and kept the stable ReplicaSet serving.

### Final rollout state

After restoring the committed good manifest, the gateway Rollout was Healthy:

```text
Name:            gateway
Status:          Healthy
Strategy:        Canary
  Step:          6/6
  SetWeight:     100
  ActualWeight:  100
Images:          ghcr.io/n1qro/quickticket-gateway:644047234546d9f31e85b566a99f9c1653df86f4 (stable)
Replicas:
  Desired:       5
  Current:       5
  Updated:       5
  Ready:         5
  Available:     5
```

### Additional metric

Beyond error rate, I would add gateway p95 latency for the canary ReplicaSet:

```promql
histogram_quantile(
  0.95,
  sum(rate(gateway_request_duration_seconds_bucket{rs_hash="{{args.canary-hash}}"}[60s])) by (le)
)
```

Error rate catches hard failures, but latency catches slow canaries before they become outright 5xx failures. I would pair it with a strict traffic-volume check so a canary with no measurable traffic cannot pass analysis silently.
