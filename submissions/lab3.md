# Lab 3 - Monitoring, Observability & SLOs

## Task 1 - Monitoring Stack and Golden Signals

### Prometheus configuration

Created `monitoring/prometheus/prometheus.yml` with three scrape jobs:

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

rule_files:
  - "rules.yml"

scrape_configs:
  - job_name: "gateway"
    static_configs:
      - targets: ["gateway:8080"]

  - job_name: "events"
    static_configs:
      - targets: ["events:8081"]

  - job_name: "payments"
    static_configs:
      - targets: ["payments:8082"]
```

Note: on my local machine `127.0.0.1:9090` was already used by `clash`, so I used a temporary non-repo Docker Compose override for verification and exposed Prometheus as `9091:9090`. The committed course config still uses the required Prometheus service config and internal port.

### Compose ps

```text
$ docker compose -f docker-compose.yaml -f ../docker-compose.monitoring.yaml -f /tmp/lab3-prometheus-port.yaml ps
NAME               IMAGE                     COMMAND                  SERVICE      STATUS                    PORTS
app-events-1       app-events                "uvicorn main:app --..." events       Up 15 minutes             0.0.0.0:8081->8081/tcp
app-gateway-1      app-gateway               "uvicorn main:app --..." gateway      Up 15 minutes             0.0.0.0:3080->8080/tcp
app-grafana-1      grafana/grafana:13.0.1    "/run.sh"                grafana      Up 18 minutes             0.0.0.0:3000->3000/tcp
app-payments-1     app-payments              "uvicorn main:app --..." payments     Up 36 seconds             0.0.0.0:8082->8082/tcp
app-postgres-1     postgres:17-alpine        "docker-entrypoint.s..." postgres     Up 18 minutes (healthy)   0.0.0.0:5432->5432/tcp
app-prometheus-1   prom/prometheus:v3.11.2   "/bin/prometheus --c..." prometheus   Up 15 minutes             0.0.0.0:9091->9090/tcp
app-redis-1        redis:7-alpine            "docker-entrypoint.s..." redis        Up 18 minutes (healthy)   0.0.0.0:6379->6379/tcp
```

### Prometheus targets

```text
$ curl -s http://127.0.0.1:9091/api/v1/targets | python3 -c '...'
events       up       http://events:8081/metrics
gateway      up       http://gateway:8080/metrics
payments     up       http://payments:8082/metrics
```

### Custom metrics

```text
$ curl -s http://127.0.0.1:9091/api/v1/label/__name__/values | python3 -c '...'
events_db_pool_size
events_orders_created
events_orders_total
events_request_duration_seconds_bucket
events_request_duration_seconds_count
events_request_duration_seconds_created
events_request_duration_seconds_sum
events_requests_created
events_requests_total
events_reservations_active
gateway_request_duration_seconds_bucket
gateway_request_duration_seconds_count
gateway_request_duration_seconds_created
gateway_request_duration_seconds_sum
gateway_requests_created
gateway_requests_total
payments_charges_created
payments_charges_total
payments_request_duration_seconds_bucket
payments_request_duration_seconds_count
payments_request_duration_seconds_created
payments_request_duration_seconds_sum
payments_requests_created
payments_requests_total
```

### Traffic generation and request rate

```text
$ ./loadgen/run.sh 5 20
QuickTicket Load Generator
Target: http://localhost:3080 | RPS: 5 | Duration: 20s
---
[10s] requests=32 success=32 fail=0 error_rate=0%
[10s] requests=33 success=33 fail=0 error_rate=0%
[10s] requests=34 success=34 fail=0 error_rate=0%
---
Done. total=65 success=65 fail=0 error_rate=0%
```

```text
$ curl -s --data-urlencode 'query=sum(rate(gateway_requests_total[5m]))' http://127.0.0.1:9091/api/v1/query | python3 -c '...'
Request rate: 0.18 req/s
```

### Dashboard panels

Latency panel queries:

```promql
histogram_quantile(0.50, sum(rate(gateway_request_duration_seconds_bucket[1m])) by (le))
histogram_quantile(0.95, sum(rate(gateway_request_duration_seconds_bucket[1m])) by (le))
histogram_quantile(0.99, sum(rate(gateway_request_duration_seconds_bucket[1m])) by (le))
```

Saturation panel query:

```promql
events_db_pool_size
```

The latency panel is a time series in seconds. The saturation panel is a gauge with min `0`, max `10`, yellow threshold at `7`, and red threshold at `9`.

### Failure observation: stopping payments

Scenario:

```text
$ ./loadgen/run.sh 5 60
$ docker compose -f docker-compose.yaml -f ../docker-compose.monitoring.yaml -f /tmp/lab3-prometheus-port.yaml stop payments
```

Observed output:

```text
payments up: 0
Availability SLO gauge: 100.00%

Done. total=211 success=211 fail=0 error_rate=0%
```

The first golden signal to show the failure was Service Health (`up{job="payments"} = 0`). It appeared after the next Prometheus scrape, about 15-20 seconds after stopping `payments`.

During this specific random loadgen run no checkout request hit `payments` after the stop, so user-facing gateway error rate stayed at 0%. Browse and reservation traffic continued to work.

## Task 2 - SLOs and Recording Rules

### SLI/SLO definitions

Availability SLI:

```promql
sum(rate(gateway_requests_total{status!~"5.."}[5m]))
/
sum(rate(gateway_requests_total[5m]))
```

Availability SLO: 99.5% over 7 days.

Latency SLI:

```promql
sum(rate(gateway_request_duration_seconds_bucket{le="0.5"}[5m]))
/
sum(rate(gateway_request_duration_seconds_count[5m]))
```

Latency SLO: 95% of gateway requests under 500ms.

Error budget math:

```text
1000 requests/day * 7 days = 7000 requests/week
Allowed unavailability = 1 - 0.995 = 0.005 = 0.5%
7000 * 0.005 = 35 failed 5xx requests/week
```

### Recording rules

Created `monitoring/prometheus/rules.yml` with:

```text
gateway:sli_availability:ratio_rate5m
gateway:sli_latency_500ms:ratio_rate5m
gateway:error_budget_burn_rate:ratio_rate5m
```

Rules loaded output:

```text
$ curl -s http://127.0.0.1:9091/api/v1/rules | python3 -c '...'
gateway:sli_availability:ratio_rate5m         = ok
gateway:sli_latency_500ms:ratio_rate5m        = ok
gateway:error_budget_burn_rate:ratio_rate5m   = ok
```

SLO gauge query:

```promql
gateway:sli_availability:ratio_rate5m * 100
```

Baseline observation:

```text
Availability SLO gauge: 100.00%
```

During the payments-stop scenario the scrape health dropped first, while the SLO gauge stayed at 100% because the random load did not send a checkout request during the outage window.

## Bonus Task - Failure Correlation Across Metrics and Logs

### Scenario

Started traffic:

```text
$ ./loadgen/run.sh 5 120
```

After 30 seconds, restarted `payments` with:

```text
PAYMENT_FAILURE_RATE=0.5
PAYMENT_LATENCY_MS=1000
```

Injection timestamp:

```text
2026-06-19T16:56:17+03:00
```

Container logs use UTC, so this corresponds to approximately `2026-06-19 13:56:17`.

### Metrics during the incident

```text
Gateway 5xx error rate: 0.25%
Gateway p95 latency: 0.025s
payments_charges_total{result="success"} 2
payments_charges_total{result="failed"} 2
```

The load generator saw user-visible failures:

```text
Done. total=412 success=375 fail=37 error_rate=8.9%
```

Some loadgen failures were 409 reservation conflicts caused by the repeated test traffic consuming small-event inventory; the payment-specific root cause is visible in the payments and gateway logs below.

### Timeline

| Time (UTC) | Event |
|------------|-------|
| 13:56:17 | `payments` recreated with 50% failure rate and 1000ms latency |
| 13:56:56 | first logged injected payment latency |
| 13:57:03 | first injected payment failure returned 500 |
| 13:57:05 | second injected payment failure returned 500 |
| after next scrape | dashboard error rate showed a small 5xx spike; service stayed `up` |
| after reset | `payments` recreated with `PAYMENT_FAILURE_RATE=0.0`, `PAYMENT_LATENCY_MS=0` |

### Log excerpts

Payments logs:

```text
payments-1 | {"time":"2026-06-19 13:56:56,995","level":"INFO","service":"payments","msg":"Injecting 1000ms latency for 98718188-9904-4f5a-a93f-25461e0ca3b2"}
payments-1 | {"time":"2026-06-19 13:56:57,995","level":"INFO","service":"payments","msg":"Payment success: PAY-AED84553 for 98718188-9904-4f5a-a93f-25461e0ca3b2"}
payments-1 | {"time":"2026-06-19 13:57:02,110","level":"INFO","service":"payments","msg":"Injecting 1000ms latency for 04e79a40-0ab0-45c4-96e6-8ab1b2e19168"}
payments-1 | {"time":"2026-06-19 13:57:03,110","level":"WARNING","service":"payments","msg":"Payment failed (injected) for 04e79a40-0ab0-45c4-96e6-8ab1b2e19168"}
payments-1 | INFO:     172.25.0.8:41566 - "POST /charge HTTP/1.1" 500 Internal Server Error
```

Gateway logs:

```text
gateway-1 | {"time":"2026-06-19 13:56:58,005","level":"INFO","service":"gateway","msg":"HTTP Request: POST http://payments:8082/charge "HTTP/1.1 200 OK""}
gateway-1 | INFO:     172.25.0.1:34258 - "POST /reserve/98718188-9904-4f5a-a93f-25461e0ca3b2/pay HTTP/1.1" 200 OK
gateway-1 | {"time":"2026-06-19 13:57:03,112","level":"INFO","service":"gateway","msg":"HTTP Request: POST http://payments:8082/charge "HTTP/1.1 500 Internal Server Error""}
gateway-1 | INFO:     172.25.0.1:47416 - "POST /reserve/04e79a40-0ab0-45c4-96e6-8ab1b2e19168/pay HTTP/1.1" 500 Internal Server Error
gateway-1 | {"time":"2026-06-19 13:57:05,874","level":"INFO","service":"gateway","msg":"HTTP Request: POST http://payments:8082/charge "HTTP/1.1 500 Internal Server Error""}
gateway-1 | INFO:     172.25.0.1:47488 - "POST /reserve/84802bcf-1aaa-42fb-a00d-c33b8c5c0c36/pay HTTP/1.1" 500 Internal Server Error
```

### Root cause

The root cause was the deliberate fault injection in the `payments` service. The service stayed reachable, so `up{job="payments"}` remained healthy, but checkout calls experienced 1000ms latency and intermittent 500 responses. Gateway propagated those failed `/charge` responses to users as 500 responses on `/reserve/{id}/pay`, which produced the error-rate spike.
