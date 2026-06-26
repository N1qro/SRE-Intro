# Lab 6 - Alerting & Incident Response

## Task 1 - Alerts, Runbook, and Incident Response

### Stack and traffic

Started QuickTicket with the monitoring stack from `app/`:

```bash
docker compose -f docker-compose.yaml -f ../docker-compose.monitoring.yaml -f /tmp/lab6-prometheus-port.yaml up -d --build
```

Host port `9090` was already in use on my machine, so I used a temporary non-repo Compose override and exposed Prometheus on `9091:9090`. Grafana still used the internal datasource URL `http://prometheus:9090`.

Running services:

```text
app-events-1       Up
app-gateway-1      Up
app-grafana-1      Up on 0.0.0.0:3000
app-payments-1     Up
app-postgres-1     Up (healthy)
app-prometheus-1   Up on 0.0.0.0:9091
app-redis-1        Up (healthy)
```

Prometheus targets were healthy:

```text
events    up    http://events:8081/metrics
gateway   up    http://gateway:8080/metrics
payments  up    http://payments:8082/metrics
```

Generated background traffic:

```bash
./loadgen/run.sh 10 900
```

Baseline gateway health:

```json
{"status":"healthy","checks":{"events":"ok","payments":"ok","circuit_payments":"CLOSED"}}
```

### Contact point and notification policy

Contact point:

```text
Name: quickticket-alerts
Type: Webhook
URL: http://lab6-webhook:8000/grafana
```

I used a local webhook receiver container on the `app_default` Docker network so Grafana could deliver real webhook notifications without relying on an external site.

Notification policy:

```text
receiver: quickticket-alerts
group_by: ["alertname"]
group_wait: 30s
group_interval: 5m
repeat_interval: 5m
```

Contact point evidence from webhook receiver:

```text
2026-06-26T18:39:10.282999+00:00 POST /grafana ... "title":"[FIRING:1] QuickTicket High Error Rate (QuickTicket critical)" ... "summary":"Gateway error rate is 5.442176870748299%"
2026-06-26T18:39:15.230103+00:00 POST /grafana ... "title":"[FIRING:1] QuickTicket SLO Burn Rate (QuickTicket warning)" ... "summary":"Gateway SLO burn rate is 8.631333357737152x"
2026-06-26T18:44:10.287376+00:00 POST /grafana ... "title":"[RESOLVED] QuickTicket High Error Rate (QuickTicket critical)" ... "summary":"Gateway error rate is 2.5500910746812386%"
```

### Alert rules

Grafana folder: `QuickTicket`

Rule group: `lab6-alerts`

Evaluation interval: `60s`

#### Alert 1 - QuickTicket High Error Rate

PromQL:

```promql
sum(rate(gateway_requests_total{status=~"5.."}[5m])) / sum(rate(gateway_requests_total[5m])) * 100
```

Configuration:

```text
Condition: above 5
Pending period: 2m
Label: severity=critical
Summary: Gateway error rate is {{ $value }}%
Description: Error rate exceeded 5% for 2 minutes. Check payments service health.
```

Grafana state evidence:

```text
21:37:22+03:00  QuickTicket High Error Rate  Pending  value 5.674456825731608%
21:39:10+03:00  Webhook notification received: [FIRING:1] QuickTicket High Error Rate, value 5.442176870748299%
21:44:10+03:00  Webhook notification received: [RESOLVED] QuickTicket High Error Rate, value 2.5500910746812386%
```

#### Alert 2 - QuickTicket SLO Burn Rate

PromQL:

```promql
(1 - (sum(rate(gateway_requests_total{status!~"5.."}[30m])) / sum(rate(gateway_requests_total[30m])))) / (1 - 0.995)
```

Configuration:

```text
Condition: above 6
Pending period: 5m
Label: severity=warning
Summary: Gateway SLO burn rate is {{ $value }}x
Description: Availability error budget burn rate exceeded 6x over 30 minutes.
```

Grafana state evidence:

```text
21:33:40+03:00  QuickTicket SLO Burn Rate  Pending  value 8.559841070189293x
21:39:15+03:00  Webhook notification received: [FIRING:1] QuickTicket SLO Burn Rate, value 8.631333357737152x
21:44:10+03:00  QuickTicket SLO Burn Rate still Alerting, value 7.262083536474678x
```

The burn-rate alert stayed active after the fix because it uses a 30-minute rolling window. That behavior is expected: it measures error budget burn over a longer interval than the critical 5-minute error-rate alert.

### Runbook: QuickTicket High Error Rate

#### Alert

- Fires when: Gateway 5xx error rate is above 5% for 2 minutes
- Dashboard: QuickTicket - Golden Signals
- Severity: Critical

#### Diagnosis

1. Check which service is failing:

   ```bash
   curl -s http://localhost:3080/health | python3 -m json.tool
   ```

2. Check payments directly:

   ```bash
   curl -s -m 3 http://localhost:8082/health
   ```

3. Check events directly:

   ```bash
   curl -s http://localhost:8081/health
   ```

4. Check recent gateway logs:

   ```bash
   docker compose -f docker-compose.yaml -f ../docker-compose.monitoring.yaml logs gateway --tail=20 --since=5m
   ```

5. Check recent payments logs:

   ```bash
   docker compose -f docker-compose.yaml -f ../docker-compose.monitoring.yaml logs payments --tail=20 --since=5m
   ```

6. Confirm the metric value in Prometheus:

   ```bash
   curl -s --data-urlencode 'query=sum(rate(gateway_requests_total{status=~"5.."}[5m])) / sum(rate(gateway_requests_total[5m])) * 100' http://127.0.0.1:9091/api/v1/query
   ```

#### Common causes

| Cause | How to identify | Fix |
|-------|-----------------|-----|
| Payments service down | Gateway health shows `payments: down`; direct payments health does not connect | Restart payments: `PAYMENT_FAILURE_RATE=0.0 docker compose -f docker-compose.yaml -f ../docker-compose.monitoring.yaml up -d payments` |
| Payments injected failures | Payments health is reachable but shows non-zero `failure_rate`; logs show injected payment failures | Recreate payments with `PAYMENT_FAILURE_RATE=0.0` |
| Events service down | Gateway health shows `events: down`; events health fails | Restart events after PostgreSQL and Redis are healthy |
| Redis down | Events health shows `redis` failure; reservations fail or time out | Restart Redis, then verify events health |
| PostgreSQL degraded | Events health shows PostgreSQL errors; browsing and reservations fail | Restart PostgreSQL and events; check database logs |

#### Mitigation

For the simulated payments outage:

```bash
PAYMENT_FAILURE_RATE=0.0 docker compose -f docker-compose.yaml -f ../docker-compose.monitoring.yaml up -d payments
```

Then verify:

```bash
curl -s http://localhost:3080/health
curl -s http://localhost:8082/health
```

Expected healthy responses:

```json
{"status":"healthy","checks":{"events":"ok","payments":"ok","circuit_payments":"CLOSED"}}
{"status":"healthy","failure_rate":0.0,"latency_ms":0}
```

#### Escalation

- If the alert is still firing 10 minutes after restoring payments, escalate to the instructor or TA.
- Include the Grafana alert state, Prometheus query result, gateway health output, and recent service logs.

### Incident simulation and response

Failure injected:

```text
2026-06-26T21:32:19+03:00
docker compose -f docker-compose.yaml -f ../docker-compose.monitoring.yaml -f /tmp/lab6-prometheus-port.yaml stop payments
```

Diagnosis output:

```json
{"status":"degraded","checks":{"events":"ok","payments":"down","circuit_payments":"CLOSED"}}
```

Direct payments health failed to connect while payments was stopped.

Events health remained healthy:

```json
{"status":"healthy","checks":{"postgres":"ok","redis":"ok"}}
```

Fix applied:

```text
2026-06-26T21:41:24+03:00
PAYMENT_FAILURE_RATE=0.0 docker compose -f docker-compose.yaml -f ../docker-compose.monitoring.yaml -f /tmp/lab6-prometheus-port.yaml up -d payments
```

Recovery verification:

```json
{"status":"healthy","checks":{"events":"ok","payments":"ok","circuit_payments":"CLOSED"}}
{"status":"healthy","failure_rate":0.0,"latency_ms":0}
```

### Timeline

| Time (+03:00) | Event |
|---------------|-------|
| 21:31 | Background load was running against the gateway. |
| 21:32:19 | Payments service was stopped to inject the incident. |
| 21:36:40 | Grafana marked `QuickTicket High Error Rate` pending. |
| 21:37:22 | API check showed `QuickTicket High Error Rate` pending with value `5.674456825731608%`. |
| 21:38:40 | Critical alert condition had been true for the 2-minute pending period. |
| 21:39:10 | Webhook received `[FIRING:1] QuickTicket High Error Rate`, value `5.442176870748299%`. |
| 21:39:15 | Webhook received `[FIRING:1] QuickTicket SLO Burn Rate`, value `8.631333357737152x`. |
| 21:40:52 | Runbook diagnosis confirmed gateway degraded with `payments: down`; events was healthy. |
| 21:41:24 | Payments was restored with `PAYMENT_FAILURE_RATE=0.0`. |
| 21:41 | Gateway and payments health checks were healthy again. |
| 21:44:10 | Webhook received `[RESOLVED] QuickTicket High Error Rate`, value `2.5500910746812386%`. |

### Alert delay answer

Failure injection to high-error firing notification took 6 minutes and 51 seconds: from `21:32:19+03:00` to `21:39:10+03:00`.

The delay came from four things:

- The alert query uses a 5-minute rate window, so the first outage requests were averaged with earlier healthy traffic.
- Grafana evaluates the rule group every 60 seconds.
- The high-error alert has a 2-minute pending period before it can fire.
- The notification policy has a 30-second group wait before sending the webhook.

## Task 2 - Blameless Postmortem

# Postmortem: Payments Outage Triggered Gateway Error-Rate Alert

**Date:** 2026-06-26

**Duration:** 21:32:19 -> 21:44:10 +03:00

**Severity:** SEV-3

**Author:** N1qro

### Summary

The payments service was intentionally stopped during background load, causing checkout requests through the gateway to fail. Browse and reservation traffic continued, but paid checkout was unavailable until payments was restored.

### Timeline

| Time (+03:00) | Event |
|---------------|-------|
| 21:32:19 | Payments service was stopped. |
| 21:36:40 | High error-rate alert entered pending state. |
| 21:39:10 | High error-rate alert fired and sent a webhook notification. |
| 21:39:15 | SLO burn-rate alert fired and sent a webhook notification. |
| 21:40:52 | Investigation confirmed gateway degraded because payments was down. |
| 21:41:24 | Payments service was restored. |
| 21:41 | Gateway and payments health checks returned healthy. |
| 21:44:10 | High error-rate alert resolved. |

### Impact

Users could still browse events and make reservations, but checkout attempts failed while payments was unavailable. During the load test, the load generator reached a cumulative failure rate above 10%, and the Grafana high-error alert fired at a measured gateway 5xx rate of `5.442176870748299%`.

### Root cause

The immediate technical cause was the payments container being stopped. The systemic cause is that checkout depends synchronously on the payments service, and the gateway returns a 5xx response when payments is unavailable. The high-error alert detected the impact only after enough failed checkout traffic accumulated in the 5-minute rate window and the 2-minute pending period elapsed.

### What went well

- The webhook contact point delivered real firing notifications for both the high-error and burn-rate alerts.
- The runbook quickly identified payments as the failing dependency.
- The gateway health endpoint clearly showed `payments: down` while events stayed healthy.
- The critical alert resolved after payments was restored.

### What went wrong

- The high-error alert initially produced a `DatasourceNoData` notification before any 5xx time series existed.
- A pure gateway error-rate alert detected user-visible impact, but it did not identify payments directly without runbook checks.
- The 30-minute burn-rate alert remained active after the fix, which is correct but could confuse responders without a note in the runbook.

### Action items

| Action | Owner | Priority |
|--------|-------|----------|
| Add a direct payments availability alert on `up{job="payments"} == 0` or gateway health dependency status. | N1qro | High |
| Change the high-error query or no-data handling so missing 5xx series is treated as zero before the first failure. | N1qro | High |
| Add a runbook note explaining that burn-rate alerts resolve slowly because of the 30-minute window. | N1qro | Medium |
| Add a dashboard panel for checkout-only error rate so payment failures are easier to isolate. | N1qro | Medium |

### Most important action item

The most important action item is adding a direct payments availability alert. The high-error alert caught customer impact, but a dependency-health alert would identify the failing service faster and reduce diagnosis time during checkout incidents.
