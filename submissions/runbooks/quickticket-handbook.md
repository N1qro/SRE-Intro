# QuickTicket SRE Handbook

## Architecture

```text
Locust / users
      |
      v
gateway Rollout, 5 replicas
      |
      +--> events Deployment, 2 replicas ----> PostgreSQL Deployment + PVC
      |                 |
      |                 +---------------------> Redis holds
      |
      +--> payments Deployment, 1 replica

Prometheus scrapes gateway metrics for canary analysis and reliability review.
ArgoCD syncs Kubernetes manifests from Git.
GitHub Actions builds images and updates image tags after merges.
```

- Gateway owns the public HTTP API and routes to events and payments.
- Events owns event listing, reservations, confirmations, Redis holds, and Postgres writes.
- Payments is intentionally simple and supports failure/latency injection for labs.
- PostgreSQL is PVC-backed after Lab 9; backups are handled by the `postgres-backup` CronJob.
- Gateway is deployed as an Argo Rollouts canary with Prometheus AnalysisRuns.

## How To Deploy

1. Work on one lowercase lab branch, for example `feature/lab10`.
2. Keep the PR diff limited to that lab's files.
3. Push the branch and open a PR against the fork and the course repo as required by the course.
4. After merge to `main`, GitHub Actions builds gateway, events, and payments images.
5. CI commits updated image tags to the Kubernetes manifests.
6. ArgoCD syncs the `k8s/` manifests into the cluster.
7. Verify:

```bash
kubectl get pods,svc
kubectl get rollout gateway
kubectl argo rollouts get rollout gateway
kubectl get application quickticket -n argocd
```

If ArgoCD is temporarily paused for local evidence collection, restore self-heal afterward:

```bash
kubectl patch application quickticket -n argocd --type=merge \
  -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}'
```

## Monitoring

Primary checks:

```bash
kubectl get pods
kubectl get rollout gateway
kubectl get analysisrun
kubectl top pods -l app=gateway
kubectl top pods -l app=events
kubectl top pods -l app=payments
```

Prometheus queries:

```promql
sum(rate(gateway_requests_total{status=~"5.."}[5m])) / sum(rate(gateway_requests_total[5m]))
histogram_quantile(0.99, sum by (le, path) (rate(gateway_request_duration_seconds_bucket[5m])))
sum(rate(gateway_requests_total[1m]))
sum by (pod) (rate(gateway_requests_total[1m]))
```

Alert priorities:

- Page on gateway 5xx ratio above 0.5% for 2 minutes.
- Page on gateway p99 latency above 500 ms for 2 minutes.
- Warn on failed AnalysisRuns.
- Warn if the newest Postgres backup is older than the expected backup interval.

## Incident Response

1. Confirm customer impact:

```bash
kubectl get pods
kubectl get rollout gateway
kubectl exec -n monitoring deployment/prometheus -- wget -qO- \
  'http://localhost:9090/api/v1/query?query=sum(rate(gateway_requests_total%7Bstatus%3D~%225..%22%7D%5B5m%5D))'
```

2. Identify the failing path:

```promql
sum by (path, status) (rate(gateway_requests_total[5m]))
histogram_quantile(0.99, sum by (le, path) (rate(gateway_request_duration_seconds_bucket[5m])))
```

3. Check dependencies:

```bash
kubectl logs deployment/events --tail=100
kubectl logs deployment/payments --tail=100
kubectl exec -i $(kubectl get pod -l app=redis -o name) -- redis-cli PING
kubectl exec -i $(kubectl get pod -l app=postgres -o name) -- \
  psql -U quickticket -d quickticket -c 'SELECT count(*) FROM events;'
```

4. If a canary is bad, abort it:

```bash
kubectl argo rollouts abort gateway
kubectl argo rollouts get rollout gateway
```

5. If the bad change already reached Git, revert through Git and let ArgoCD sync:

```bash
git revert <bad-commit>
git push origin <branch>
```

6. If errors continue after the dependency is restored, restart stale DB clients:

```bash
kubectl rollout restart deployment/events
kubectl rollout status deployment/events --timeout=180s
```

## Backup And Restore

Create an on-demand custom-format backup:

```bash
kubectl exec -i $(kubectl get pod -l app=postgres -o name) -- \
  pg_dump -U quickticket -Fc quickticket > /tmp/quickticket.dump
ls -lh /tmp/quickticket.dump
file /tmp/quickticket.dump
```

Inspect the backup:

```bash
POD=$(kubectl get pod -l app=postgres -o name | cut -d/ -f2)
kubectl cp /tmp/quickticket.dump $POD:/tmp/backup.dump
kubectl exec $POD -- pg_restore --list /tmp/backup.dump
```

Restore:

```bash
POD=$(kubectl get pod -l app=postgres -o name | cut -d/ -f2)
kubectl cp /tmp/quickticket.dump $POD:/tmp/backup.dump
kubectl exec $POD -- pg_restore -U quickticket -d quickticket --clean --if-exists /tmp/backup.dump
kubectl rollout restart deployment/events
kubectl rollout status deployment/events --timeout=180s
```

Verify row counts and smoke test:

```bash
kubectl exec $POD -- psql -U quickticket -d quickticket \
  -c 'SELECT count(*) FROM events; SELECT count(*) FROM orders;'
```

Operational notes:

- The PVC prevents data loss on ordinary Postgres pod replacement.
- The backup CronJob reduces RPO but does not replace restore drills.
- For production, add WAL archiving or managed point-in-time recovery.
