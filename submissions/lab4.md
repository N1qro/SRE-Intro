# Lab 4 - Kubernetes: Deploy QuickTicket to a Cluster

## Task 1 - Kubernetes Manifests and k3d Deployment

### Tooling and cluster

`kubectl`, `k3d`, and `helm` were installed locally under `~/.local/bin`.

```text
$ kubectl version --client=true
Client Version: v1.36.2
Kustomize Version: v5.8.1

$ k3d version
k3d version v5.8.3
k3s version v1.31.5-k3s1 (default)

$ helm version --short
v3.18.3+g6838ebc
```

Created the cluster:

```text
$ k3d cluster create quickticket
```

Node status:

```text
$ kubectl get nodes
NAME                       STATUS   ROLES                  AGE   VERSION
k3d-quickticket-server-0   Ready    control-plane,master   24m   v1.31.5+k3s1
```

### Images

Built and imported the three local service images:

```text
$ docker build -t quickticket-gateway:v1 ./gateway
$ docker build -t quickticket-events:v1 ./events
$ docker build -t quickticket-payments:v1 ./payments
$ k3d image import quickticket-gateway:v1 quickticket-events:v1 quickticket-payments:v1 -c quickticket
```

Verification:

```text
$ docker images | grep quickticket
quickticket-gateway    v1    9cf0e7c34724    143MB
quickticket-events     v1    22e3265c356b    157MB
quickticket-payments   v1    2c3359f42c07    141MB
```

The k3s system pods initially could not pull a few images because of registry timeouts, so I imported the required system images into k3d as well:

```text
rancher/mirrored-pause:3.6
rancher/mirrored-coredns-coredns:1.12.0
rancher/klipper-helm:v0.9.3-build20241008
rancher/local-path-provisioner:v0.0.30
rancher/mirrored-metrics-server:v0.7.2
```

After that, CoreDNS became available and service DNS worked normally.

### Manifests

Created raw manifests in `k8s/`:

```text
k8s/postgres.yaml
k8s/redis.yaml
k8s/events.yaml
k8s/payments.yaml
k8s/gateway.yaml
```

Each file contains a Deployment and a ClusterIP Service. The three QuickTicket services use local images with `imagePullPolicy: Never`.

Important service DNS names:

```text
postgres:5432
redis:6379
events:8081
payments:8082
gateway:8080
```

Applied the manifests:

```text
$ kubectl apply -f k8s/
deployment.apps/events created
service/events created
deployment.apps/gateway created
service/gateway created
deployment.apps/payments created
service/payments created
deployment.apps/postgres created
service/postgres created
deployment.apps/redis created
service/redis created
```

Because Kubernetes has no `depends_on`, I restarted `events` and `gateway` after PostgreSQL, Redis, and payments were available:

```text
$ kubectl rollout restart deployment/events deployment/gateway
deployment.apps/events restarted
deployment.apps/gateway restarted
```

### Running pods and services

```text
$ kubectl get pods,svc
NAME                            READY   STATUS    RESTARTS   AGE
pod/events-f95df554f-tfbt2      1/1     Running   0          2m53s
pod/gateway-6fc8958468-fvqnp    1/1     Running   0          2m52s
pod/payments-5d4557d7ff-ld9g4   1/1     Running   0          2m53s
pod/postgres-85ffd4fb9f-pbz2b   1/1     Running   0          21m
pod/redis-6d65768944-x6nnn      1/1     Running   0          21m

NAME                 TYPE        CLUSTER-IP      EXTERNAL-IP   PORT(S)    AGE
service/events       ClusterIP   10.43.57.92     <none>        8081/TCP   21m
service/gateway      ClusterIP   10.43.201.12    <none>        8080/TCP   21m
service/kubernetes   ClusterIP   10.43.0.1       <none>        443/TCP    25m
service/payments     ClusterIP   10.43.127.79    <none>        8082/TCP   21m
service/postgres     ClusterIP   10.43.193.252   <none>        5432/TCP   21m
service/redis        ClusterIP   10.43.120.116   <none>        6379/TCP   21m
```

### Database initialization

```text
$ kubectl exec -i deployment/postgres -- psql -U quickticket -d quickticket -f /dev/stdin < app/seed.sql
CREATE TABLE
CREATE TABLE
INSERT 0 5
```

### Port-forward and app verification

```text
$ kubectl port-forward svc/gateway 3080:8080
Forwarding from 127.0.0.1:3080 -> 8080
Forwarding from [::1]:3080 -> 8080
```

Events endpoint:

```json
[
    {
        "id": 1,
        "name": "Go Conference 2026",
        "venue": "Main Hall A",
        "date": "2026-09-15T09:00:00+00:00",
        "total_tickets": 100,
        "price_cents": 5000,
        "available": 100
    },
    {
        "id": 4,
        "name": "Python Workshop",
        "venue": "Lab 301",
        "date": "2026-09-22T14:00:00+00:00",
        "total_tickets": 25,
        "price_cents": 2000,
        "available": 25
    },
    {
        "id": 2,
        "name": "SRE Meetup",
        "venue": "Room 204",
        "date": "2026-10-01T18:00:00+00:00",
        "total_tickets": 30,
        "price_cents": 0,
        "available": 30
    },
    {
        "id": 5,
        "name": "Kubernetes Deep Dive",
        "venue": "Auditorium B",
        "date": "2026-10-10T10:00:00+00:00",
        "total_tickets": 80,
        "price_cents": 8000,
        "available": 80
    },
    {
        "id": 3,
        "name": "Cloud Native Summit",
        "venue": "Expo Center",
        "date": "2026-11-20T10:00:00+00:00",
        "total_tickets": 500,
        "price_cents": 15000,
        "available": 500
    }
]
```

Health endpoint:

```json
{
    "status": "healthy",
    "checks": {
        "events": "ok",
        "payments": "ok",
        "circuit_payments": "CLOSED"
    }
}
```

### Self-healing test

Deleted the gateway pod:

```text
$ kubectl delete pod gateway-6fc8958468-fvqnp
pod "gateway-6fc8958468-fvqnp" deleted
```

Watch samples:

```text
Deleting gateway-6fc8958468-fvqnp at 2026-06-19T19:34:31+03:00

gateway-6fc8958468-6pxgg   0/1   ContainerCreating   0   3s
gateway-6fc8958468-6pxgg   0/1   Running             0   8s
gateway-6fc8958468-6pxgg   0/1   Running             0   34s
gateway-6fc8958468-6pxgg   0/1   Running             0   60s
gateway-6fc8958468-6pxgg   1/1   Running             0   66s

Recovered with gateway-6fc8958468-6pxgg at 2026-06-19T19:35:38+03:00 in 67s
```

Kubernetes recreated the deleted pod automatically. The pod was created almost immediately and became Ready after 67 seconds, mostly because the readiness probe has a 60 second startup delay. In docker-compose, I had to manually start a stopped service; Kubernetes handled replacement by itself through the Deployment controller.

## Task 2 - Probes and Resource Limits

### Probes

Gateway probes:

```text
Liveness:   http-get http://:8080/health delay=60s timeout=1s period=10s #success=1 #failure=3
Readiness:  http-get http://:8080/health delay=60s timeout=1s period=5s #success=1 #failure=2
```

Events probes:

```text
Liveness:   http-get http://:8081/health delay=60s timeout=1s period=10s #success=1 #failure=3
Readiness:  http-get http://:8081/health delay=60s timeout=1s period=5s #success=1 #failure=2
```

Payments has the same pattern on port `8082`.

I used `initialDelaySeconds: 60` for gateway and events because their `/health` endpoints depend on downstream services. With a shorter delay, startup ordering and temporary DNS unavailability caused premature liveness failures.

### Resource limits

Each container has:

```yaml
resources:
  requests:
    cpu: 50m
    memory: 64Mi
  limits:
    cpu: 200m
    memory: 256Mi
```

Node allocation:

```text
$ kubectl describe node k3d-quickticket-server-0 | grep -A 12 "Allocated resources"
Allocated resources:
  (Total limits may be over 100 percent, i.e., overcommitted.)
  Resource           Requests    Limits
  --------           --------    ------
  cpu                450m (11%)  1 (25%)
  memory             460Mi (5%)  1450Mi (18%)
  ephemeral-storage  0 (0%)      0 (0%)
  hugepages-1Gi      0 (0%)      0 (0%)
  hugepages-2Mi      0 (0%)      0 (0%)
```

The totals include QuickTicket pods and cluster/system pods.

### Readiness failure during Redis outage

Deleting only the Redis pod recreated it too quickly to keep Events unready:

```text
Deleting redis pod redis-6d65768944-x6nnn at 2026-06-19T19:38:07+03:00
redis-6d65768944-szvtp   1/1   Running   0   6s
events-f95df554f-tfbt2   1/1   Running   0   8m52s
```

To observe the readiness behavior clearly, I held Redis down briefly:

```text
$ kubectl scale deployment/redis --replicas=0
deployment.apps/redis scaled

events-f95df554f-tfbt2   1/1   Running   0          11m
events-f95df554f-tfbt2   0/1   Running   0          12m
events-f95df554f-tfbt2   0/1   Running   0          12m
events-f95df554f-tfbt2   0/1   Running   1 (4s ago) 12m

$ kubectl scale deployment/redis --replicas=1
deployment.apps/redis scaled
deployment.apps/redis condition met

events-f95df554f-tfbt2   0/1   Running   1 (58s ago)   13m
events-f95df554f-tfbt2   1/1   Running   1 (63s ago)   13m
```

Events changed to `0/1 Ready`, so Kubernetes would remove it from Service endpoints until readiness recovered. Because this lab's liveness probe also uses `/health`, the longer Redis outage eventually restarted Events too. That is a useful warning: dependency checks are safer as readiness checks, not liveness checks.

### Liveness vs readiness answer

Readiness failure means the pod is alive but should not receive traffic; Kubernetes removes it from Service endpoints and keeps the process running. Liveness failure means Kubernetes treats the container as stuck and restarts it.

Database or Redis connectivity should be checked with readiness, not liveness. If a dependency is down, restarting the app pod usually does not fix the dependency; it only adds churn. Readiness lets the pod recover in place when the dependency comes back.

## Bonus Task - Helm Chart

### Chart files

Created a Helm chart in `k8s/chart/`:

```text
k8s/chart/Chart.yaml
k8s/chart/values.yaml
k8s/chart/templates/postgres.yaml
k8s/chart/templates/redis.yaml
k8s/chart/templates/events.yaml
k8s/chart/templates/payments.yaml
k8s/chart/templates/gateway.yaml
```

`Chart.yaml`:

```yaml
apiVersion: v2
name: quickticket
description: QuickTicket SRE learning project
version: 0.1.0
```

Key values:

```yaml
gateway:
  replicas: 1
  image: quickticket-gateway:v1
events:
  replicas: 1
  image: quickticket-events:v1
payments:
  replicas: 1
  image: quickticket-payments:v1
postgres:
  image: postgres:17-alpine
redis:
  image: redis:7-alpine
```

Rendered successfully:

```text
$ helm template quickticket k8s/chart
# rendered Services and Deployments for gateway, events, payments, postgres, redis
```

### Helm install

Deleted raw manifests and installed the chart:

```text
$ kubectl delete -f k8s/
deployment.apps "events" deleted
service "events" deleted
deployment.apps "gateway" deleted
service "gateway" deleted
deployment.apps "payments" deleted
service "payments" deleted
deployment.apps "postgres" deleted
service "postgres" deleted
deployment.apps "redis" deleted
service "redis" deleted

$ helm install quickticket k8s/chart/
NAME: quickticket
LAST DEPLOYED: Fri Jun 19 19:43:56 2026
NAMESPACE: default
STATUS: deployed
REVISION: 1
TEST SUITE: None
```

Pods after Helm install:

```text
$ kubectl get pods
NAME                        READY   STATUS    RESTARTS   AGE
events-c4549cd77-rvnqb      1/1     Running   0          90s
gateway-54cd55d84d-5bkgh    1/1     Running   0          90s
payments-6df867965d-jsrhf   1/1     Running   0          107s
postgres-85ffd4fb9f-2d2q7   1/1     Running   0          107s
redis-6d65768944-s4p8b      1/1     Running   0          107s
```

Helm releases:

```text
$ helm list
NAME        NAMESPACE  REVISION  UPDATED                                  STATUS    CHART             APP VERSION
quickticket default    1         2026-06-19 19:43:56.918656341 +0300 MSK  deployed  quickticket-0.1.0
```

I loaded `seed.sql` again after the Helm install and verified the application through the gateway service:

```text
$ kubectl exec -i deployment/postgres -- psql -U quickticket -d quickticket -f /dev/stdin < app/seed.sql
CREATE TABLE
CREATE TABLE
INSERT 0 5

$ curl -s http://127.0.0.1:3080/health | python3 -m json.tool
{
    "status": "healthy",
    "checks": {
        "events": "ok",
        "payments": "ok",
        "circuit_payments": "CLOSED"
    }
}
```

### Monitoring via Helm

Installed kube-prometheus-stack:

```text
$ helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
$ helm install monitoring prometheus-community/kube-prometheus-stack \
    --set grafana.adminPassword=admin \
    --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false
NAME: monitoring
STATUS: deployed
CHART: kube-prometheus-stack-86.3.2
APP VERSION: v0.91.0
```

Helm releases after monitoring install:

```text
$ helm list
NAME        NAMESPACE  REVISION  STATUS    CHART                         APP VERSION
monitoring  default    1         deployed  kube-prometheus-stack-86.3.2   v0.91.0
quickticket default    1         deployed  quickticket-0.1.0
```

Monitoring-related pods created:

```text
$ kubectl get pods
alertmanager-monitoring-kube-prometheus-alertmanager-0   2/2   Running
monitoring-grafana-65f747d54c-8fg72                      2/3   ImagePullBackOff
monitoring-kube-prometheus-operator-6fb54b476d-6w89g     1/1   Running
monitoring-kube-state-metrics-8588bb4b88-dj45x           0/1   ErrImagePull
monitoring-prometheus-node-exporter-xczmr                1/1   Running
prometheus-monitoring-kube-prometheus-prometheus-0       2/2   Running
```

kube-prometheus-stack created 6 monitoring-related pods. Four were running or mostly running; two had image-pull issues caused by network timeouts to `registry.k8s.io` / Google artifact registry:

```text
Failed to pull image "registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.19.1":
dial tcp ...:443: i/o timeout
```

The QuickTicket Helm chart itself was installed and verified successfully.
