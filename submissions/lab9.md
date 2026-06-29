# Lab 9 - Stateful Services and DB Reliability

## Setup

I worked on branch `feature/lab9` against the local `quickticket` k3d cluster.

```bash
k3d cluster start quickticket
export KUBECONFIG=/home/n1qro/.config/k3d/kubeconfig-quickticket.yaml
kubectl get nodes
```

ArgoCD was still configured for automated self-heal from an older branch, so I paused it while collecting local Lab 9 evidence:

```bash
kubectl patch application quickticket -n argocd --type=merge -p '{"spec":{"syncPolicy":null}}'
```

The cluster Postgres pod had ephemeral data at the start of the lab, so I reseeded it and raised ticket counts to avoid artificial sellouts while mixed load was running:

```bash
kubectl exec -i $(kubectl get pod -l app=postgres -o name) -- \
  psql -U quickticket -d quickticket < app/seed.sql

kubectl exec $(kubectl get pod -l app=postgres -o name) -- \
  psql -U quickticket -d quickticket -c 'UPDATE events SET total_tickets = 10000;'
```

Seed output:

```text
CREATE TABLE
CREATE TABLE
INSERT 0 5
UPDATE 5
```

The provided Lab 8 mixed load image had pull problems in my local cluster, so I used the same live fallback pattern from Lab 8: a small Python loop in the already available gateway image that calls `GET /events`, `POST /events/1/reserve`, and `POST /reserve/{id}/pay`.

```text
NAME        READY   UP-TO-DATE   AVAILABLE   AGE
mixedload   2/2     2            2           5m50s
```

I created a Python 3.13 virtual environment because the system `python3` is 3.8, then installed the lab versions:

```bash
python3.13 -m venv .venv
.venv/bin/pip install alembic==1.18.4 psycopg2-binary==2.9.11 sqlalchemy==2.0.49
```

Postgres was exposed to Alembic with:

```bash
kubectl port-forward svc/postgres 5432:5432
```

Connection check:

```bash
.venv/bin/python - <<'PY'
import psycopg2
c = psycopg2.connect('postgresql://quickticket:quickticket@localhost:5432/quickticket')
cur = c.cursor()
cur.execute('SELECT count(*) FROM events')
print('events:', cur.fetchone()[0])
PY
```

Output:

```text
events: 5
```

## Task 1 - Migrations and Backup/Restore

### Alembic setup

I initialized Alembic in `migrations/` and configured `alembic.ini`:

```ini
sqlalchemy.url = postgresql://quickticket:quickticket@localhost:5432/quickticket
```

The migration history has two deterministic revisions: `0001` is the baseline for the existing schema and `0002` adds a nullable `email` column to `events`.

```bash
.venv/bin/alembic history
```

Output:

```text
0001 -> 0002 (head), add email column to events
<base> -> 0001, baseline pre existing schema
```

I stamped the live pre-existing schema as the baseline:

```bash
.venv/bin/alembic stamp 0001
.venv/bin/alembic current
```

Output:

```text
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running stamp_revision  -> 0001

INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
0001
```

### Migration under load

Baseline gateway 5xx before the migration:

```bash
kubectl exec -n monitoring deployment/prometheus -- wget -qO- \
  'http://localhost:9090/api/v1/query?query=sum(increase(gateway_requests_total%7Bstatus%3D~%225..%22%7D%5B1m%5D))'
```

Output:

```text
{"status":"success","data":{"resultType":"vector","result":[{"metric":{},"value":[1782743896.941,"0"]}]}}
```

Then I ran the nullable-column migration while mixed load was running:

```bash
/usr/bin/time -p .venv/bin/alembic upgrade head
```

Output:

```text
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running upgrade 0001 -> 0002, add email column to events
real 5.77
user 0.84
sys 0.09
```

Current revision after the migration:

```bash
.venv/bin/alembic current
```

Output:

```text
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
0002 (head)
```

Schema verification:

```bash
kubectl exec -i $(kubectl get pod -l app=postgres -o name) -- \
  psql -U quickticket -d quickticket -c '\d events'
```

Output:

```text
                                        Table "public.events"
    Column     |           Type           | Collation | Nullable |              Default
---------------+--------------------------+-----------+----------+------------------------------------
 id            | integer                  |           | not null | nextval('events_id_seq'::regclass)
 name          | text                     |           | not null |
 venue         | text                     |           | not null |
 event_date    | timestamp with time zone |           | not null |
 total_tickets | integer                  |           | not null |
 price_cents   | integer                  |           | not null |
 email         | character varying(255)   |           |          |
Indexes:
    "events_pkey" PRIMARY KEY, btree (id)
Referenced by:
    TABLE "orders" CONSTRAINT "orders_event_id_fkey" FOREIGN KEY (event_id) REFERENCES events(id)
```

Gateway 5xx after the migration stayed unchanged:

```bash
kubectl exec -n monitoring deployment/prometheus -- wget -qO- \
  'http://localhost:9090/api/v1/query?query=sum(increase(gateway_requests_total%7Bstatus%3D~%225..%22%7D%5B1m%5D))'
```

Output:

```text
{"status":"success","data":{"resultType":"vector","result":[{"metric":{},"value":[1782743968.109,"0"]}]}}
```

The migration was safe under load because it added a nullable column. PostgreSQL does not need to rewrite every row for this change.

### Backup

I created a custom-format dump from inside the Postgres pod:

```bash
kubectl exec -i $(kubectl get pod -l app=postgres -o name) -- \
  pg_dump -U quickticket -Fc quickticket > /tmp/quickticket.dump
ls -lh /tmp/quickticket.dump
file /tmp/quickticket.dump
```

Output:

```text
-rw-rw-r-- 1 n1qro n1qro 71K Jun 29 17:39 /tmp/quickticket.dump
/tmp/quickticket.dump: PostgreSQL custom database dump - v1.16-0
```

I copied the dump back into the Postgres pod and inspected it with `pg_restore --list`:

```bash
POD=$(kubectl get pod -l app=postgres -o name | cut -d/ -f2)
kubectl cp /tmp/quickticket.dump $POD:/tmp/backup.dump
kubectl exec $POD -- pg_restore --list /tmp/backup.dump
```

Output:

```text
;
; Archive created at 2026-06-29 14:39:50 UTC
;     dbname: quickticket
;     TOC Entries: 18
;     Compression: gzip
;     Dump Version: 1.16-0
;     Format: CUSTOM
;     Integer: 4 bytes
;     Offset: 8 bytes
;     Dumped from database version: 17.10
;     Dumped by pg_dump version: 17.10
;
;
; Selected TOC Entries:
;
220; 1259 16414 TABLE public alembic_version quickticket
218; 1259 16386 TABLE public events quickticket
217; 1259 16385 SEQUENCE public events_id_seq quickticket
3481; 0 0 SEQUENCE OWNED BY public events_id_seq quickticket
219; 1259 16394 TABLE public orders quickticket
3316; 2604 16389 DEFAULT public events id quickticket
3474; 0 16414 TABLE DATA public alembic_version quickticket
3472; 0 16386 TABLE DATA public events quickticket
3473; 0 16394 TABLE DATA public orders quickticket
3482; 0 0 SEQUENCE SET public events_id_seq quickticket
3324; 2606 16418 CONSTRAINT public alembic_version alembic_version_pkc quickticket
3320; 2606 16393 CONSTRAINT public events events_pkey quickticket
3322; 2606 16402 CONSTRAINT public orders orders_pkey quickticket
3325; 2606 16403 FK CONSTRAINT public orders orders_event_id_fkey quickticket
```

### Simulated data loss and restore

Before the table drop:

```bash
kubectl exec $POD -- psql -U quickticket -d quickticket \
  -c 'SELECT count(*) AS events_count FROM events; SELECT count(*) AS orders_count FROM orders;'
```

Output:

```text
 events_count
--------------
            5
(1 row)

 orders_count
--------------
         2118
(1 row)
```

I dropped the `orders` table:

```bash
kubectl exec $POD -- psql -U quickticket -d quickticket -c 'DROP TABLE orders CASCADE'
```

Output:

```text
DROP TABLE
```

Row count check after the drop:

```bash
kubectl exec $POD -- psql -U quickticket -d quickticket \
  -c 'SELECT count(*) AS events_count FROM events; SELECT count(*) AS orders_count FROM orders;'
```

Output:

```text
ERROR:  relation "orders" does not exist
LINE 1: ...nt FROM events; SELECT count(*) AS orders_count FROM orders;
                                                                ^
 events_count
--------------
            5
(1 row)

command terminated with exit code 1
```

In-cluster smoke probe while the table was missing:

```bash
kubectl run smoke \
  --image=ghcr.io/n1qro/quickticket-gateway:bd0118c82395970520aa51464877c757c330f704 \
  --image-pull-policy=IfNotPresent --rm -i --restart=Never --quiet \
  --command -- python -c '...'
```

Output:

```text
events 502
reserve 405
```

The reserve probe used `GET`, so the `405` only proves the route exists but requires a different method. The `/events` `502` showed user-visible impact while the backing database schema was broken.

Restore command:

```bash
kubectl exec $POD -- pg_restore -U quickticket -d quickticket --clean --if-exists /tmp/backup.dump
kubectl rollout restart deployment/events
kubectl rollout status deployment/events --timeout=120s
```

Counts after restore:

```text
 events_count
--------------
            5
(1 row)

 orders_count
--------------
         1917
(1 row)
```

For the table-drop exercise, the backup restored the schema and service, but it restored the database to the backup point. The visible row gap was `2118 - 1917 = 201` orders, which is the practical RPO cost of taking a single periodic dump.

To improve this RPO, I would use more frequent automated dumps at minimum, and for a production setup I would add WAL archiving or managed database point-in-time recovery.

## Task 2 - Disaster Recovery Under Load

For the first disaster test, Postgres was still running without a PVC. I kept mixed load running and killed the Postgres pod.

```bash
POD=$(kubectl get pod -l app=postgres -o jsonpath="{.items[0].metadata.name}")
kubectl exec "$POD" -- psql -U quickticket -d quickticket \
  -c "SELECT count(*) AS events_count FROM events; SELECT count(*) AS orders_count FROM orders;"
T0=$(date +%H:%M:%S)
kubectl delete pod -l app=postgres --grace-period=0 --force
T_KILL=$(date +%H:%M:%S)
kubectl wait --for=condition=Ready pod -l app=postgres --timeout=120s
```

Start evidence:

```text
postgres pod before disaster: postgres-85ffd4fb9f-2d2q7
counts before disaster:
 events_count
--------------
            5
(1 row)

 orders_count
--------------
         2635
(1 row)

healthy at 17:46:00
pod "postgres-85ffd4fb9f-2d2q7" force deleted from default namespace
disaster at 17:46:00
pod/postgres-85ffd4fb9f-bb558 condition met
new pod ready at 17:46:37: postgres-85ffd4fb9f-bb558
```

The new pod was not immediately ready for `psql`, and the first restore attempt happened too early:

```text
new pod relations before restore:
psql: error: connection to server on socket "/var/run/postgresql/.s.PGSQL.5432" failed: No such file or directory
	Is the server running locally and accepting connections on that socket?
command terminated with exit code 2
pg_restore: error: connection to server on socket "/var/run/postgresql/.s.PGSQL.5432" failed: No such file or directory
	Is the server running locally and accepting connections on that socket?
command terminated with exit code 1
```

After the pod settled, the database was empty, proving the no-PVC failure mode:

```bash
kubectl exec "$NEW_POD" -- psql -U quickticket -d quickticket -c '\dt'
```

Output:

```text
Did not find any relations.
```

I then restored the backup for real:

```bash
kubectl cp /tmp/quickticket.dump "$NEW_POD":/tmp/backup.dump
kubectl exec "$NEW_POD" -- pg_restore -U quickticket -d quickticket --clean --if-exists /tmp/backup.dump
kubectl rollout restart deployment/events
kubectl rollout status deployment/events --timeout=180s
```

Recovery output:

```text
real restore start at 17:49:15
real restore finished at 17:49:18
deployment.apps/events restarted
deployment "events" successfully rolled out
post-restore app check finished at 17:51:40
counts after real restore:
 events_count
--------------
            5
(1 row)

 orders_count
--------------
         2429
(1 row)

timestamps: restore_start=17:49:15 restored=17:49:18 app_ready=17:51:40
```

Prometheus 5xx rate around the incident:

```bash
kubectl exec -n monitoring deployment/prometheus -- wget -qO- \
  'http://localhost:9090/api/v1/query?query=sum(rate(gateway_requests_total%7Bstatus%3D~%225..%22%7D%5B30s%5D))'
```

Output:

```text
{"status":"success","data":{"resultType":"vector","result":[{"metric":{},"value":[1782744722.694,"0.04"]}]}}
```

RTO and RPO:

- Disaster started at `17:46:00`.
- Replacement pod was reported ready at `17:46:37`, but Postgres was not yet accepting `psql` connections.
- Real restore finished at `17:49:18`.
- Events rollout completed at `17:51:40`.
- Actual RTO for this run was `340s`.
- Orders before disaster: `2635`.
- Orders after restore: `2429`.
- Row-level RPO gap: `206` orders.

The new Postgres pod was empty because the original Deployment stored database files inside the pod filesystem. Killing the pod killed the data. The way to eliminate this failure mode is to mount durable storage, which I implemented in the bonus task with a PVC.

## Bonus Task - Persistent Storage and Automated Backups

### Postgres PVC

I updated `k8s/postgres.yaml` to add `PGDATA`, mount `/var/lib/postgresql/data`, and create a `postgres-data` PVC.

```bash
git diff -- k8s/postgres.yaml
```

Diff:

```diff
@@ -26,6 +26,11 @@ spec:
               value: "quickticket"
             - name: POSTGRES_PASSWORD
               value: "quickticket"
+            - name: PGDATA
+              value: "/var/lib/postgresql/data/pgdata"
+          volumeMounts:
+            - name: data
+              mountPath: /var/lib/postgresql/data
           resources:
             requests:
               cpu: 50m
@@ -33,6 +38,20 @@ spec:
             limits:
               cpu: 200m
               memory: 256Mi
+      volumes:
+        - name: data
+          persistentVolumeClaim:
+            claimName: postgres-data
+---
+apiVersion: v1
+kind: PersistentVolumeClaim
+metadata:
+  name: postgres-data
+spec:
+  accessModes: [ReadWriteOnce]
+  resources:
+    requests:
+      storage: 1Gi
 ---
 apiVersion: v1
 kind: Service
```

Apply output:

```bash
kubectl apply -f k8s/postgres.yaml
kubectl rollout status deployment/postgres --timeout=180s
```

```text
deployment.apps/postgres configured
persistentvolumeclaim/postgres-data created
service/postgres configured
NAME            STATUS   VOLUME                                     CAPACITY   ACCESS MODES   STORAGECLASS   AGE
postgres-data   Bound    pvc-11790a36-4b4e-4aaf-b4fd-94f39add1516   1Gi        RWO            local-path     6m46s
```

The first PVC apply needed one local environmental fix: k3d could not pull the local-path helper image from Docker Hub, so I imported it into the cluster.

```bash
docker pull rancher/mirrored-library-busybox:1.36.1
k3d image import rancher/mirrored-library-busybox:1.36.1 -c quickticket
```

After the PVC-backed Postgres pod started, it was a fresh volume, so I reseeded once and applied the Alembic migration to the new persistent database:

```text
pvc postgres pod: postgres-6fc5585b5b-87g8s
relations before seed:
Did not find any relations.
CREATE TABLE
CREATE TABLE
INSERT 0 5
UPDATE 5
 events_count
--------------
            5
(1 row)

 orders_count
--------------
            0
(1 row)
```

### PVC restart proof

I repeated the pod-kill test with the PVC-backed deployment:

```bash
kubectl delete pod -l app=postgres --grace-period=0 --force
kubectl wait --for=condition=Ready pod -l app=postgres --timeout=120s
kubectl exec "$NEW_POD" -- psql -U quickticket -d quickticket -c '\dt'
```

Output:

```text
pvc pod before restart: postgres-6fc5585b5b-87g8s
counts before pvc restart:
 events_count
--------------
            5
(1 row)

 orders_count
--------------
         1556
(1 row)

pod "postgres-6fc5585b5b-87g8s" force deleted from default namespace
pvc disaster at 18:06:07
pod/postgres-6fc5585b5b-p2x62 condition met
pvc new pod ready at 18:06:14: postgres-6fc5585b5b-p2x62
```

The first immediate `psql` call hit PostgreSQL recovery:

```text
FATAL:  the database system is not yet accepting connections
DETAIL:  Consistent recovery state has not been yet reached.
```

After the pod settled, the tables were still present without any restore:

```text
settled pvc pod: postgres-6fc5585b5b-p2x62
               List of relations
 Schema |      Name       | Type  |    Owner
--------+-----------------+-------+-------------
 public | alembic_version | table | quickticket
 public | events          | table | quickticket
 public | orders          | table | quickticket
(3 rows)

 events_count
--------------
            5
(1 row)

 orders_count
--------------
         2235
(1 row)
```

PVC-backed RTO:

- Disaster: `18:06:07`.
- Pod condition met: `18:06:14`.
- Events rollout completed: `18:08:42`.
- Practical RTO with app reconnect: `155s`.
- Manual `pg_restore` step required: none.

This improves the failure mode from "empty database plus manual restore" to "Postgres restart/recovery plus application reconnection." The tradeoff is that the cluster now depends on PV health and consumes persistent disk.

### Backup CronJob

I wrote `k8s/backup-cronjob.yaml`:

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: postgres-backup
spec:
  schedule: "*/5 * * * *"
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: OnFailure
          containers:
            - name: pg-dump
              image: postgres:17-alpine
              env:
                - name: PGHOST
                  value: postgres
                - name: PGUSER
                  value: quickticket
                - name: PGDATABASE
                  value: quickticket
                - name: PGPASSWORD
                  value: quickticket
              command:
                - sh
                - -c
                - |
                  set -eu
                  cd /backups
                  ts="$(date -u +%Y%m%dT%H%M%SZ)_${HOSTNAME}"
                  dump="quickticket_${ts}.dump"
                  pg_dump -Fc -f "$dump"
                  echo "created $dump"
                  ls -1t quickticket_*.dump | tail -n +6 | while read -r old; do
                    rm -v "$old"
                  done
                  echo "remaining backups:"
                  ls -1t quickticket_*.dump
              volumeMounts:
                - name: backups
                  mountPath: /backups
          volumes:
            - name: backups
              persistentVolumeClaim:
                claimName: postgres-backups
```

I applied the provided backup storage and the CronJob:

```bash
kubectl apply -f labs/lab9/backup-storage.yaml
kubectl rollout status deployment/backup-inspector --timeout=120s
kubectl get pvc postgres-backups
kubectl apply -f k8s/backup-cronjob.yaml
```

The backup inspector initially could not pull `alpine:3.20`, so I imported that image into k3d:

```bash
docker pull alpine:3.20
k3d image import alpine:3.20 -c quickticket
```

Then the inspector rolled out and the backup PVC was ready:

```text
deployment "backup-inspector" successfully rolled out
NAME               STATUS   VOLUME                                     CAPACITY   ACCESS MODES   STORAGECLASS
postgres-backups   Bound    pvc-23f7b7af-7142-452f-8481-10f952a8ecaa   1Gi        RWO            local-path
cronjob.batch/postgres-backup created
```

I triggered seven manual jobs:

```bash
for i in 1 2 3 4 5 6 7; do
  kubectl create job --from=cronjob/postgres-backup manual-$i
  kubectl wait --for=condition=Complete job/manual-$i --timeout=120s
  sleep 1
done
```

Output:

```text
creating manual-1
job.batch/manual-1 created
job.batch/manual-1 condition met
creating manual-2
job.batch/manual-2 created
job.batch/manual-2 condition met
creating manual-3
job.batch/manual-3 created
job.batch/manual-3 condition met
creating manual-4
job.batch/manual-4 created
job.batch/manual-4 condition met
creating manual-5
job.batch/manual-5 created
job.batch/manual-5 condition met
creating manual-6
job.batch/manual-6 created
job.batch/manual-6 condition met
creating manual-7
job.batch/manual-7 created
job.batch/manual-7 condition met
```

`manual-7` logs showed retention deleting an older dump:

```bash
kubectl logs job/manual-7
```

Output:

```text
created quickticket_20260629T151438Z_manual-7-wh8wz.dump
removed 'quickticket_20260629T151354Z_manual-2-dpllt.dump'
remaining backups:
quickticket_20260629T151438Z_manual-7-wh8wz.dump
quickticket_20260629T151430Z_manual-6-vnr6h.dump
quickticket_20260629T151421Z_manual-5-mfsww.dump
quickticket_20260629T151411Z_manual-4-ztznf.dump
quickticket_20260629T151403Z_manual-3-djsrb.dump
```

Final backup directory listing:

```bash
kubectl exec deployment/backup-inspector -- ls -la /backups
```

Output:

```text
total 448
drwxrwxrwx    2 root     root          4096 Jun 29 15:15 .
drwxr-xr-x    1 root     root          4096 Jun 29 15:12 ..
-rw-r--r--    1 root     root         88011 Jun 29 15:14 quickticket_20260629T151411Z_manual-4-ztznf.dump
-rw-r--r--    1 root     root         88011 Jun 29 15:14 quickticket_20260629T151421Z_manual-5-mfsww.dump
-rw-r--r--    1 root     root         88011 Jun 29 15:14 quickticket_20260629T151430Z_manual-6-vnr6h.dump
-rw-r--r--    1 root     root         88011 Jun 29 15:14 quickticket_20260629T151438Z_manual-7-wh8wz.dump
-rw-r--r--    1 root     root         88011 Jun 29 15:15 quickticket_20260629T151502Z_postgres-backup-29712435-xkh24.dump
```

There are exactly five retained dumps. A scheduled CronJob run fired after the manual sequence, replacing one manual dump with a scheduled dump, which also confirms the schedule works.

## Answers

The RPO of a single `pg_dump` is the time and writes since that dump was created. In my run, the no-PVC disaster lost 206 orders compared with the count immediately before the incident. More frequent automated backups reduce that window; WAL archiving or managed point-in-time recovery would reduce it much further.

The no-PVC Postgres pod came back empty because Kubernetes recreated the pod with a new container filesystem. The PVC fixes that specific failure mode by putting the database files on persistent storage that survives pod replacement.

The CronJob improves recovery options but it is not a full database reliability solution by itself. It should be paired with restore drills, monitoring for backup freshness, alerts on failed jobs, and a storage/replication design appropriate for production.
