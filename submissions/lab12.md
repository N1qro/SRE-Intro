# Lab 12 - Advanced Kubernetes Resilience

## Setup

I started the existing `quickticket` k3d cluster and selected its kubeconfig. The Kubernetes API needed a short warm-up while the old controllers and pods recovered.

```text
$ k3d cluster start quickticket
$ export KUBECONFIG=/home/n1qro/.config/k3d/kubeconfig-quickticket.yaml
$ kubectl get nodes
NAME                       STATUS   ROLES                  AGE   VERSION
k3d-quickticket-server-0   Ready    control-plane,master   28d   v1.31.5+k3s1
```

I paused ArgoCD self-healing, removed the stale gateway Deployment left from an earlier lab, and applied the current manifests. The gateway remained an Argo Rollout.

```text
$ kubectl patch application quickticket -n argocd --type=merge -p '{"spec":{"syncPolicy":null}}'
application.argoproj.io/quickticket patched
$ kubectl delete deployment gateway --ignore-not-found=true
deployment.apps "gateway" deleted from default namespace
$ kubectl apply -f k8s/redis.yaml -f k8s/postgres.yaml -f k8s/events.yaml \
    -f k8s/payments.yaml -f k8s/analysis-template.yaml -f k8s/gateway.yaml \
    -f k8s/notifications.yaml -f k8s/pdb.yaml
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
deployment.apps/notifications created
service/notifications created
poddisruptionbudget.policy/gateway-pdb created
poddisruptionbudget.policy/events-pdb created
poddisruptionbudget.policy/payments-pdb created
poddisruptionbudget.policy/notifications-pdb created
```

The Lab 11 notifications image was not present in GHCR, and container-network dependency installation timed out. I downloaded the pinned wheels into `/tmp`, built the image from an offline temporary context, and imported it without adding build files to the repository.

```text
$ docker pull ghcr.io/n1qro/quickticket-notifications:f76fcf311c8f83beda6a68e19c3b5c2d7c891e13
Error response from daemon: manifest unknown
$ .venv/bin/pip download -d /tmp/lab12-wheels -r app/notifications/requirements.txt
Successfully downloaded fastapi uvicorn prometheus-client annotated-doc click h11 pydantic pydantic-core annotated-types starlette anyio idna typing-extensions typing-inspection
$ DOCKER_BUILDKIT=0 docker build -t quickticket-notifications:v1 /tmp/lab12-notifications-build
Successfully built e1d2787e7cc5
Successfully tagged quickticket-notifications:v1
$ k3d image import -c quickticket quickticket-notifications:v1
INFO Successfully imported 1 image(s) into 1 cluster(s)
```

I started the provided mixed load before the resilience and migration tests.

```text
$ kubectl apply -f labs/lab8/mixedload.yaml
deployment.apps/mixedload created
$ kubectl rollout status deployment/mixedload --timeout=60s
deployment "mixedload" successfully rolled out
$ kubectl exec $(kubectl get pod -l app=mixedload -o jsonpath='{.items[0].metadata.name}') -- \
    curl -s -w '\nstatus=%{http_code}\n' http://gateway:8080/health
{"status":"healthy","checks":{"events":"ok","payments":"ok","notifications":"ok","circuit_payments":"CLOSED"}}
status=200
```

## Task 1 - Multi-Replica Failover and PDBs

### Replica counts

Events already had two replicas from the current main branch. I changed payments and notifications from one to two replicas.

```text
$ kubectl get deploy -l 'app in (events,payments,notifications)'
NAME            READY   UP-TO-DATE   AVAILABLE   AGE
events          2/2     2            2           28d
notifications   2/2     2            2           6m2s
payments        2/2     2            2           28d
$ kubectl get rollout gateway
NAME      DESIRED   CURRENT   UP-TO-DATE   AVAILABLE   AGE
gateway   5         5         5            5           19d
```

### Failover under load

The one-minute baseline had no gateway 5xx responses.

```text
$ kubectl exec -n monitoring deployment/prometheus -- wget -qO- \
    'http://localhost:9090/api/v1/query?query=sum(increase(gateway_requests_total%7Bstatus%3D~%225..%22%7D%5B1m%5D))+or+vector(0)'
{"status":"success","data":{"resultType":"vector","result":[{"metric":{},"value":[1784319965.304,"0"]}]}}
```

I deleted one gateway pod and one events pod while mixed load continued. The events replacement took longer than five seconds because its existing readiness probe has a 60-second initial delay, but the surviving replicas served traffic throughout recovery.

```text
$ GW=$(kubectl get pod -l app=gateway -o jsonpath='{.items[0].metadata.name}')
$ EV=$(kubectl get pod -l app=events -o jsonpath='{.items[0].metadata.name}')
$ echo "start=$(date -Iseconds) gateway=$GW events=$EV"
start=2026-07-17T23:26:29+03:00 gateway=gateway-7d74645c55-h2kl4 events=events-6d786cb649-4kwsp
$ kubectl delete pod "$GW" --wait=false
pod "gateway-7d74645c55-h2kl4" deleted from default namespace
$ kubectl delete pod "$EV" --wait=false
pod "events-6d786cb649-4kwsp" deleted from default namespace
$ kubectl wait --for=condition=Ready pod -l app=gateway --timeout=75s
pod/gateway-7d74645c55-hm84z condition met
$ kubectl wait --for=condition=Ready pod -l app=events --timeout=75s
pod/events-6d786cb649-hc6t9 condition met
$ echo "recovered=$(date -Iseconds)"
recovered=2026-07-17T23:27:46+03:00
```

The post-failover query also returned zero.

```text
$ kubectl exec -n monitoring deployment/prometheus -- wget -qO- \
    'http://localhost:9090/api/v1/query?query=sum(increase(gateway_requests_total%7Bstatus%3D~%225..%22%7D%5B2m%5D))+or+vector(0)'
{"status":"success","data":{"resultType":"vector","result":[{"metric":{},"value":[1784320095.254,"0"]}]}}
```

### PodDisruptionBudgets

I added four PDBs in `k8s/pdb.yaml`:

```yaml
gateway-pdb:
  minAvailable: 2
events-pdb:
  minAvailable: 1
payments-pdb:
  minAvailable: 1
notifications-pdb:
  maxUnavailable: 1
```

All budgets reported the expected allowed disruptions after the replicas were ready.

```text
$ kubectl get pdb
NAME                MIN AVAILABLE   MAX UNAVAILABLE   ALLOWED DISRUPTIONS
events-pdb          1               N/A               1
gateway-pdb         2               N/A               3
notifications-pdb   N/A             1                 1
payments-pdb        1               N/A               1
```

To prove enforcement, I temporarily changed `events-pdb` to `minAvailable: 2`, which reduced allowed disruptions to zero. I then sent an eviction request through the Kubernetes API and restored the original value.

```text
$ kubectl patch pdb events-pdb --type=merge -p '{"spec":{"minAvailable":2}}'
poddisruptionbudget.policy/events-pdb patched
$ kubectl get pdb events-pdb
NAME         MIN AVAILABLE   MAX UNAVAILABLE   ALLOWED DISRUPTIONS
events-pdb   2               N/A               0
$ kubectl proxy --port=8901 >/tmp/lab12-proxy.log 2>&1 &
$ PROXY_PID=$!
$ curl -s -i -X POST -H 'Content-Type: application/json' \
    --data-binary @/tmp/lab12-eviction.json \
    http://localhost:8901/api/v1/namespaces/default/pods/events-6d786cb649-hc6t9/eviction
HTTP/1.1 429 Too Many Requests
Content-Type: application/json

{
  "kind": "Status",
  "status": "Failure",
  "message": "Cannot evict pod as it would violate the pod's disruption budget.",
  "reason": "TooManyRequests",
  "details": {
    "causes": [
      {
        "reason": "DisruptionBudget",
        "message": "The disruption budget events-pdb needs 2 healthy pods and has 2 currently"
      }
    ]
  },
  "code": 429
}
$ kill "$PROXY_PID"
$ kubectl patch pdb events-pdb --type=merge -p '{"spec":{"minAvailable":1}}'
poddisruptionbudget.policy/events-pdb patched
```

With three gateway replicas and `minAvailable: 1`, at most two pods can be voluntarily evicted at once. My gateway has five replicas and `minAvailable: 2`, so it can tolerate three voluntary evictions while retaining two serving pods. Keeping two instead of one preserves more useful capacity during maintenance without blocking a drain as aggressively as `minAvailable: 4` would.

### Topology spread

I added a preferred hostname spread with `maxSkew: 1` and `ScheduleAnyway`. The live Rollout contained the expected configuration.

```text
$ kubectl get rollout gateway -o jsonpath='{.spec.template.spec.topologySpreadConstraints}' | python3 -m json.tool
[
    {
        "labelSelector": {"matchLabels": {"app": "gateway"}},
        "maxSkew": 1,
        "topologyKey": "kubernetes.io/hostname",
        "whenUnsatisfiable": "ScheduleAnyway"
    }
]
$ kubectl get pod -l app=gateway -o wide
NAME                       READY   STATUS    NODE
gateway-7d74645c55-hkvtj   1/1     Running   k3d-quickticket-server-0
gateway-7d74645c55-hm84z   1/1     Running   k3d-quickticket-server-0
gateway-7d74645c55-kmt74   1/1     Running   k3d-quickticket-server-0
gateway-7d74645c55-pv5sf   1/1     Running   k3d-quickticket-server-0
gateway-7d74645c55-r6lbb   1/1     Running   k3d-quickticket-server-0
```

All pods remain on one node because this k3d cluster has one node. On a three-node cluster, five gateway pods would be placed `2/2/1`. Seven pods would be placed `3/2/2`; both distributions keep the difference between the largest and smallest group at one.

## Task 2 - Graceful Shutdown and Zero-Downtime Migration

### Gateway shutdown behavior

I added a 40-second termination grace period, a 10-second `preStop` delay, and a fast readiness probe:

```yaml
terminationGracePeriodSeconds: 40
containers:
  - name: gateway
    lifecycle:
      preStop:
        exec:
          command: ["sh", "-c", "sleep 10"]
    readinessProbe:
      httpGet:
        path: /health
        port: 8080
      periodSeconds: 2
      failureThreshold: 1
```

The canary AnalysisRun passed before the rollout reached all five replicas. I then restarted the Rollout under mixed load. The 5xx count remained zero before and after the restart.

```text
$ kubectl exec -n monitoring deployment/prometheus -- wget -qO- \
    'http://localhost:9090/api/v1/query?query=sum(increase(gateway_requests_total%7Bstatus%3D~%225..%22%7D%5B1m%5D))+or+vector(0)'
{"status":"success","data":{"resultType":"vector","result":[{"metric":{},"value":[1784320197.422,"0"]}]}}
$ echo restart=$(date -Iseconds)
restart=2026-07-17T23:29:57+03:00
$ kubectl argo rollouts restart gateway
rollout 'gateway' restarts in 0s
$ kubectl argo rollouts status gateway --timeout=240s
Healthy
$ echo completed=$(date -Iseconds)
completed=2026-07-17T23:31:03+03:00
$ kubectl exec -n monitoring deployment/prometheus -- wget -qO- \
    'http://localhost:9090/api/v1/query?query=sum(increase(gateway_requests_total%7Bstatus%3D~%225..%22%7D%5B3m%5D))+or+vector(0)'
{"status":"success","data":{"resultType":"vector","result":[{"metric":{},"value":[1784320264.086,"0"]}]}}
```

The `preStop` delay gives endpoint updates time to propagate before Uvicorn receives SIGTERM. The remaining grace period then allows in-flight requests to finish.

### Concurrent index migration

Migration `0003` creates and drops the event-date index outside Alembic's normal transaction:

```python
def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "idx_events_event_date",
            "events",
            ["event_date"],
            unique=False,
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "idx_events_event_date",
            table_name="events",
            postgresql_concurrently=True,
            if_exists=True,
        )
```

I applied it through a temporary Postgres port-forward while mixed load continued. The total gateway 5xx counter remained zero.

```text
$ .venv/bin/alembic current
0002
$ kubectl exec -n monitoring deployment/prometheus -- wget -qO- \
    'http://localhost:9090/api/v1/query?query=sum(gateway_requests_total%7Bstatus%3D~%225..%22%7D)+or+vector(0)'
{"status":"success","data":{"resultType":"vector","result":[{"metric":{},"value":[1784320302.744,"0"]}]}}
$ /usr/bin/time -p .venv/bin/alembic upgrade 0003
INFO  [alembic.runtime.migration] Running upgrade 0002 -> 0003, index events.event_date concurrently
real 0.83
user 0.53
sys 0.06
$ kubectl exec $(kubectl get pod -l app=postgres -o jsonpath='{.items[0].metadata.name}') -- \
    psql -U quickticket -d quickticket -c '\d events'
Indexes:
    "events_pkey" PRIMARY KEY, btree (id)
    "idx_events_event_date" btree (event_date)
$ kubectl exec -n monitoring deployment/prometheus -- wget -qO- \
    'http://localhost:9090/api/v1/query?query=sum(gateway_requests_total%7Bstatus%3D~%225..%22%7D)+or+vector(0)'
{"status":"success","data":{"resultType":"vector","result":[{"metric":{},"value":[1784320304.116,"0"]}]}}
```

`CREATE INDEX CONCURRENTLY` matters because a normal index build blocks writes for the duration of the build. On a table with ten million rows, that can stall application writes for minutes and create a large request queue. The concurrent form takes longer and performs multiple scans, but normal reads and writes can continue.

### Expand-and-contract design

The zero-downtime rename requires these interleaved steps:

1. Migration 1 adds nullable `scheduled_at` without removing `event_date`.
2. Deploy A reads `COALESCE(scheduled_at, event_date)` and would dual-write both columns if QuickTicket had a runtime event-creation path. This service only seeds events at startup, so there was no runtime event write to change.
3. Migration 2 backfills `scheduled_at` from `event_date` and then makes it non-null.
4. Deploy B reads only `scheduled_at`. The row position and external `date` response field remain unchanged for compatibility.
5. Migration 3 drops `event_date` only after every Deploy A pod has terminated.

Migration 3 must come after Deploy B is fully rolled out because Deploy A still references `event_date` in `COALESCE`. Dropping the column while even one Deploy A pod remains would make every request served by that pod fail with an undefined-column database error.

## Bonus Task - Executed Expand-and-Contract Rename

### Migration 1 - Add the new column

Migration `0004` adds the nullable column:

```python
def upgrade() -> None:
    op.add_column(
        "events",
        sa.Column("scheduled_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
```

I recorded a zero baseline, applied the migration, and verified that existing rows initially had no `scheduled_at` value.

```text
$ kubectl exec -n monitoring deployment/prometheus -- wget -qO- \
    'http://localhost:9090/api/v1/query?query=sum(gateway_requests_total%7Bstatus%3D~%225..%22%7D)+or+vector(0)'
{"status":"success","data":{"resultType":"vector","result":[{"metric":{},"value":[1784320342.807,"0"]}]}}
$ .venv/bin/alembic upgrade 0004
INFO  [alembic.runtime.migration] Running upgrade 0003 -> 0004, add events.scheduled_at column
$ .venv/bin/alembic current
0004
$ kubectl exec $(kubectl get pod -l app=postgres -o jsonpath='{.items[0].metadata.name}') -- \
    psql -U quickticket -d quickticket -c 'SELECT id, event_date, scheduled_at FROM events ORDER BY id LIMIT 5;'
 id |       event_date       | scheduled_at
----+------------------------+--------------
  1 | 2026-09-15 09:00:00+00 |
  2 | 2026-10-01 18:00:00+00 |
  3 | 2026-11-20 10:00:00+00 |
  4 | 2026-09-22 14:00:00+00 |
  5 | 2026-10-10 10:00:00+00 |
(5 rows)
```

### Deploy A - Fallback read

Deploy A used both columns during the overlap window:

```sql
SELECT e.id, e.name, e.venue,
       COALESCE(e.scheduled_at, e.event_date) AS event_date,
       e.total_tickets, e.price_cents,
       COALESCE(SUM(o.quantity), 0) AS confirmed
FROM events e LEFT JOIN orders o ON e.id = o.event_id
GROUP BY e.id ORDER BY COALESCE(e.scheduled_at, e.event_date)
```

I built it as a distinct local tag, imported it, and waited for both events replicas. The total 5xx counter remained zero.

```text
$ DOCKER_BUILDKIT=0 docker build -t quickticket-events:lab12-a /tmp/lab12-events-build
Successfully built a967fb4756c2
Successfully tagged quickticket-events:lab12-a
$ k3d image import -c quickticket quickticket-events:lab12-a
INFO Successfully imported 1 image(s) into 1 cluster(s)
$ kubectl patch deployment events --type=strategic -p '{"spec":{"template":{"spec":{"containers":[{"name":"events","image":"quickticket-events:lab12-a","imagePullPolicy":"IfNotPresent"}]}}}}'
deployment.apps/events patched
$ kubectl rollout status deployment/events --timeout=150s
deployment "events" successfully rolled out
$ kubectl exec -n monitoring deployment/prometheus -- wget -qO- \
    'http://localhost:9090/api/v1/query?query=sum(gateway_requests_total%7Bstatus%3D~%225..%22%7D)+or+vector(0)'
{"status":"success","data":{"resultType":"vector","result":[{"metric":{},"value":[1784320727.864,"0"]}]}}
```

### Migration 2 - Backfill and constrain

Migration `0005` backfills only missing values and then adds the non-null constraint:

```python
def upgrade() -> None:
    op.execute(
        "UPDATE events SET scheduled_at = event_date WHERE scheduled_at IS NULL"
    )
    op.alter_column("events", "scheduled_at", nullable=False)
```

```text
$ .venv/bin/alembic upgrade 0005
INFO  [alembic.runtime.migration] Running upgrade 0004 -> 0005, backfill events.scheduled_at
$ kubectl exec $(kubectl get pod -l app=postgres -o jsonpath='{.items[0].metadata.name}') -- \
    psql -U quickticket -d quickticket -c \
    'SELECT id, event_date, scheduled_at, scheduled_at = event_date AS equal FROM events ORDER BY id LIMIT 5;'
 id |       event_date       |      scheduled_at      | equal
----+------------------------+------------------------+-------
  1 | 2026-09-15 09:00:00+00 | 2026-09-15 09:00:00+00 | t
  2 | 2026-10-01 18:00:00+00 | 2026-10-01 18:00:00+00 | t
  3 | 2026-11-20 10:00:00+00 | 2026-11-20 10:00:00+00 | t
  4 | 2026-09-22 14:00:00+00 | 2026-09-22 14:00:00+00 | t
  5 | 2026-10-10 10:00:00+00 | 2026-10-10 10:00:00+00 | t
(5 rows)
$ kubectl exec $(kubectl get pod -l app=postgres -o jsonpath='{.items[0].metadata.name}') -- \
    psql -U quickticket -d quickticket -c \
    "SELECT column_name, is_nullable FROM information_schema.columns WHERE table_name='events' AND column_name='scheduled_at';"
 column_name  | is_nullable
--------------+-------------
 scheduled_at | NO
(1 row)
```

The backfill is safe while Deploy A is live because its fallback read accepts both null and non-null `scheduled_at`. The `WHERE scheduled_at IS NULL` predicate also makes the data update idempotent.

### Deploy B - New column only

Deploy B removed the fallback and read only the new column. The SQL alias keeps the query result shape stable, and the HTTP response continues to expose the existing `date` field.

```diff
- COALESCE(e.scheduled_at, e.event_date) AS event_date
+ e.scheduled_at AS event_date
- ORDER BY COALESCE(e.scheduled_at, e.event_date)
+ ORDER BY e.scheduled_at
```

I also changed `app/seed.sql` to create and populate `scheduled_at` on a fresh database.

```text
$ DOCKER_BUILDKIT=0 docker build -t quickticket-events:lab12-b /tmp/lab12-events-build
Successfully built 71bc74bcad5c
Successfully tagged quickticket-events:lab12-b
$ k3d image import -c quickticket quickticket-events:lab12-b
INFO Successfully imported 1 image(s) into 1 cluster(s)
$ kubectl patch deployment events --type=strategic -p '{"spec":{"template":{"spec":{"containers":[{"name":"events","image":"quickticket-events:lab12-b","imagePullPolicy":"IfNotPresent"}]}}}}'
deployment.apps/events patched
$ kubectl rollout status deployment/events --timeout=150s
deployment "events" successfully rolled out
$ kubectl exec $(kubectl get pod -l app=mixedload -o jsonpath='{.items[0].metadata.name}') -- \
    curl -s -o /dev/null -w 'GET /events status=%{http_code}\n' http://gateway:8080/events
GET /events status=200
$ kubectl exec -n monitoring deployment/prometheus -- wget -qO- \
    'http://localhost:9090/api/v1/query?query=sum(gateway_requests_total%7Bstatus%3D~%225..%22%7D)+or+vector(0)'
{"status":"success","data":{"resultType":"vector","result":[{"metric":{},"value":[1784321095.887,"0"]}]}}
```

### Migration 3 - Drop the old column

Migration `0006` contracts the schema:

```python
def upgrade() -> None:
    op.drop_column("events", "event_date")


def downgrade() -> None:
    op.add_column(
        "events",
        sa.Column("event_date", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.execute("UPDATE events SET event_date = scheduled_at")
    op.alter_column("events", "event_date", nullable=False)
```

After the migration, the old column and its dependent index were gone, `scheduled_at` was non-null, and the API still returned 200.

```text
$ .venv/bin/alembic upgrade 0006
INFO  [alembic.runtime.migration] Running upgrade 0005 -> 0006, drop events.event_date
$ .venv/bin/alembic current
0006 (head)
$ kubectl exec $(kubectl get pod -l app=postgres -o jsonpath='{.items[0].metadata.name}') -- \
    psql -U quickticket -d quickticket -c '\d events'
                                        Table "public.events"
    Column     |           Type           | Nullable
---------------+--------------------------+----------
 id            | integer                  | not null
 name          | text                     | not null
 venue         | text                     | not null
 total_tickets | integer                  | not null
 price_cents   | integer                  | not null
 email         | character varying(255)   |
 scheduled_at  | timestamp with time zone | not null
Indexes:
    "events_pkey" PRIMARY KEY, btree (id)
$ kubectl exec $(kubectl get pod -l app=mixedload -o jsonpath='{.items[0].metadata.name}') -- \
    curl -s -o /dev/null -w 'GET /events status=%{http_code}\n' http://gateway:8080/events
GET /events status=200
```

I exercised the final migration downgrade and upgrade, then ran the updated seed file against the final schema. The migration returned to `0006`, the seed SQL completed, and the gateway 5xx total stayed at zero.

```text
$ .venv/bin/alembic downgrade 0005
INFO  [alembic.runtime.migration] Running downgrade 0006 -> 0005, drop events.event_date
$ .venv/bin/alembic upgrade 0006
INFO  [alembic.runtime.migration] Running upgrade 0005 -> 0006, drop events.event_date
$ .venv/bin/alembic current
0006 (head)
$ kubectl exec -i $(kubectl get pod -l app=postgres -o jsonpath='{.items[0].metadata.name}') -- \
    psql -U quickticket -d quickticket -f /dev/stdin < app/seed.sql
CREATE TABLE
CREATE TABLE
INSERT 0 5
$ kubectl exec -n monitoring deployment/prometheus -- wget -qO- \
    'http://localhost:9090/api/v1/query?query=sum(gateway_requests_total%7Bstatus%3D~%225..%22%7D)+or+vector(0)'
{"status":"success","data":{"resultType":"vector","result":[{"metric":{},"value":[1784321169.162,"0"]}]}}
```

The baseline and final values were identical across all five transitions.

```text
$ echo "baseline=$BASELINE final=$FINAL"
baseline=0 final=0
$ if [ "$BASELINE" = "$FINAL" ]; then echo 'diff: no 5xx change across the migration sequence'; fi
diff: no 5xx change across the migration sequence
```

The dangerous reordering would be running migration 3 before Deploy B is fully rolled out. That is the only step that removes a column still referenced by an earlier code version, so it would immediately create 5xx responses from any remaining Deploy A pod.

For a ten-million-row production table, I would batch the backfill by primary-key range and commit every batch:

```text
last_id = 0
repeat:
    rows = SELECT id FROM events WHERE id > last_id
           AND scheduled_at IS NULL ORDER BY id LIMIT 10000
    if rows is empty: stop
    BEGIN
    UPDATE events SET scheduled_at = event_date
    WHERE id BETWEEN rows.first.id AND rows.last.id
      AND scheduled_at IS NULL
    COMMIT
    last_id = rows.last.id
    sleep briefly
```

The migration 3 downgrade is not sufficient by itself for a production rollback after Deploy B is live. It backfills `event_date` once, but Deploy B continues using only `scheduled_at`; later writes would not be mirrored to the restored old column. A safe rollback requires a compatibility deploy that dual-writes both columns, a final backfill after dual-write is active, and confirmation that every old-code reader can use complete `event_date` data before rolling the application back.

## Validation

I parsed the final Python and YAML files, checked the complete migration chain, and used Kubernetes server-side dry-run validation before stopping the cluster.

```text
$ python3 -c 'import ast; from pathlib import Path; paths=[Path("app/events/main.py"), *Path("migrations/versions").glob("000[3-6]_*.py")]; [ast.parse(p.read_text()) for p in paths]; print("python syntax: OK")'
python syntax: OK
$ python3 -c 'import yaml; from pathlib import Path; paths=[Path("k8s/gateway.yaml"),Path("k8s/payments.yaml"),Path("k8s/notifications.yaml"),Path("k8s/pdb.yaml")]; [list(yaml.safe_load_all(p.read_text())) for p in paths]; print("yaml syntax: OK")'
yaml syntax: OK
$ .venv/bin/alembic history
0005 -> 0006 (head), drop events.event_date
0004 -> 0005, backfill events.scheduled_at
0003 -> 0004, add events.scheduled_at column
0002 -> 0003, index events.event_date concurrently
0001 -> 0002, add email column to events
<base> -> 0001, baseline pre existing schema
$ kubectl apply --dry-run=server -f k8s/gateway.yaml -f k8s/events.yaml \
    -f k8s/payments.yaml -f k8s/notifications.yaml -f k8s/pdb.yaml
rollout.argoproj.io/gateway configured (server dry run)
service/gateway unchanged (server dry run)
deployment.apps/events configured (server dry run)
service/events unchanged (server dry run)
deployment.apps/payments unchanged (server dry run)
service/payments unchanged (server dry run)
deployment.apps/notifications unchanged (server dry run)
service/notifications unchanged (server dry run)
poddisruptionbudget.policy/gateway-pdb configured (server dry run)
poddisruptionbudget.policy/events-pdb configured (server dry run)
poddisruptionbudget.policy/payments-pdb configured (server dry run)
poddisruptionbudget.policy/notifications-pdb configured (server dry run)
```

## Optional HPA Observation

I did not complete the optional HPA observation. The graded Task 1, Task 2, and Expand-and-Contract Bonus Task were completed.
