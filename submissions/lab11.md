# Lab 11 - Advanced Microservice Patterns

## Setup

I worked on branch `feature/lab11`, created from `main`.

I started the existing k3d cluster and selected its kubeconfig:

```bash
k3d cluster start quickticket
export KUBECONFIG=/home/n1qro/.config/k3d/kubeconfig-quickticket.yaml
kubectl get nodes
```

Node output:

```text
NAME                       STATUS   ROLES                  AGE   VERSION
k3d-quickticket-server-0   Ready    control-plane,master   25d   v1.31.5+k3s1
```

After the restart, the data services recovered but the old application pods remained in `Unknown` state:

```bash
kubectl get pods
```

Relevant output:

```text
NAME                                     READY   STATUS
events-77c8655cdb-xbdml                  0/1     Unknown
gateway-54f644b488-mhjvp                 0/1     Unknown
gateway-5dc9568f86-2xlwg                 0/1     Unknown
payments-8c6b8cdd5-9wbfx                 0/1     Unknown
postgres-85ffd4fb9f-2wgn2                1/1     Running
redis-6d65768944-24z7k                   1/1     Running
```

I checked Docker before rebuilding the gateway and notifications images:

```bash
timeout 15s docker info >/tmp/docker-info.out 2>&1
status=$?
echo status=$status
sed -n '1,30p' /tmp/docker-info.out
```

Output:

```text
status=124
Client: Docker Engine - Community
 Version:    28.1.1
Server:
```

The Docker daemon did not answer within 15 seconds. Because I could not build and import the Lab 11 images, I could not run the Kubernetes checkout bursts and Prometheus queries from the lab sheet. I continued with local behavior checks for the new service and the three gateway patterns.

## Task 1 - Notifications Service and Retries

### Notifications service

I added `app/notifications/main.py` using the payments service as the template. The service reads the two fault-injection variables and defines the required Prometheus metrics:

```python
NOTIFY_FAILURE_RATE = float(os.getenv("NOTIFY_FAILURE_RATE", "0.0"))
NOTIFY_LATENCY_MS = int(os.getenv("NOTIFY_LATENCY_MS", "0"))

REQUEST_COUNT = Counter(
    "notifications_requests_total", "Total requests", ["method", "path", "status"]
)
REQUEST_DURATION = Histogram(
    "notifications_request_duration_seconds", "Request duration", ["method", "path"]
)
NOTIFY_TOTAL = Counter(
    "notifications_notify_total", "Total notification attempts", ["result"]
)
```

The `/notify` endpoint applies latency first, then returns an injected 500 or a successful response:

```python
@app.post("/notify")
def notify(body: dict = None):
    body = body or {}
    event = body.get("event", "unknown")
    order_id = body.get("order_id", "unknown")

    if NOTIFY_LATENCY_MS > 0:
        log.info(f"Injecting {NOTIFY_LATENCY_MS}ms latency for {order_id}")
        time.sleep(NOTIFY_LATENCY_MS / 1000)

    if random.random() < NOTIFY_FAILURE_RATE:
        NOTIFY_TOTAL.labels("failed").inc()
        raise HTTPException(500, "Notification delivery failed")

    NOTIFY_TOTAL.labels("success").inc()
    return {"status": "sent", "event": event, "order_id": order_id}
```

The notification image uses the same dependencies as payments:

```text
fastapi==0.136.0
uvicorn==0.44.0
prometheus-client==0.25.0
```

### Kubernetes manifest and gateway wiring

I added a one-replica notifications Deployment and a ClusterIP Service on port `8083`. The image is loaded locally, so the manifest uses `imagePullPolicy: Never`.

```bash
git diff main -- k8s/notifications.yaml k8s/gateway.yaml
```

Relevant diff:

```diff
+apiVersion: apps/v1
+kind: Deployment
+metadata:
+  name: notifications
+spec:
+  replicas: 1
+  template:
+    spec:
+      containers:
+        - name: notifications
+          image: quickticket-notifications:v1
+          imagePullPolicy: Never
+          ports:
+            - containerPort: 8083
+          env:
+            - name: NOTIFY_FAILURE_RATE
+              value: "0.0"
+            - name: NOTIFY_LATENCY_MS
+              value: "0"
+---
+apiVersion: v1
+kind: Service
+metadata:
+  name: notifications
+spec:
+  type: ClusterIP
+  selector:
+    app: notifications
+  ports:
+    - name: http
+      port: 8083
+      targetPort: 8083

             - name: PAYMENTS_URL
               value: "http://payments:8082"
+            - name: NOTIFICATIONS_URL
+              value: "http://notifications:8083"
```

### Retry implementation

I replaced the no-op retry body with transient-error classification, exponential backoff, jitter, and metrics:

```python
async def call_with_retry(func, target: str, max_retries: int = RETRY_MAX):
    last_error = None
    base_delay = RETRY_BASE_DELAY_MS / 1000

    for attempt in range(max_retries):
        try:
            result = await func()
            if attempt > 0:
                RETRY_TOTAL.labels(target, "succeeded_after_retry").inc()
            return result
        except Exception as exc:
            last_error = exc
            retryable = isinstance(exc, (httpx.TimeoutException, httpx.ConnectError))
            if isinstance(exc, httpx.HTTPStatusError):
                status = exc.response.status_code
                retryable = status >= 500 or status in (408, 429)

            if not retryable:
                if isinstance(exc, httpx.HTTPStatusError) and 400 <= exc.response.status_code < 500:
                    RETRY_TOTAL.labels(target, "non_retryable").inc()
                raise

            if attempt == max_retries - 1:
                RETRY_TOTAL.labels(target, "exhausted").inc()
                raise

            RETRY_TOTAL.labels(target, "retried").inc()
            delay = base_delay * (2 ** attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)

    raise last_error
```

### Local Task 1 check

I created a temporary Python 3.13 environment under `/tmp` so the test did not add files to the repository:

```bash
python3.13 -m venv /tmp/lab11-venv
/tmp/lab11-venv/bin/pip install -r app/notifications/requirements.txt httpx==0.28.1
/tmp/lab11-venv/bin/python -c 'import fastapi, httpx; print("fastapi", fastapi.__version__); print("httpx", httpx.__version__)'
```

Output:

```text
fastapi 0.136.0
httpx 0.28.1
```

I used a temporary smoke script to call the notification function with success, failure, and latency settings, and to call a payment function that failed twice before succeeding:

```bash
timeout 20s /tmp/lab11-venv/bin/python -u /tmp/lab11_behavior_test.py 2>/dev/null
```

Task 1 output:

```text
notifications:
  success: {'status': 'sent', 'event': 'order_confirmed', 'order_id': 'order-1'}
  injected failure: (500, 'Notification delivery failed')
  50ms latency request: sent 50.3ms
  notifications_notify_total{result="success"} 2.0
  notifications_notify_total{result="failed"} 1.0
retry:
  result: charged
  attempts: 3
  retried counter: 2.0
  succeeded_after_retry counter: 1.0
```

The retry result confirms that two transient HTTP 500 responses were retried and the third attempt succeeded. The notification counters also match the two successful calls and one injected failure.

Notifications should be non-blocking because delivery is not part of the critical payment result. Waiting for a slow notification would increase `/pay` latency, and returning an error after a successful payment would give the user the wrong result. The gateway therefore schedules the helper with `asyncio.create_task()` after confirmation.

The correct composition is `cb.call(retry(_charge))`. One circuit-breaker call represents one logical payment operation, including its retry attempts, so the breaker sees only the final result. With `retry(cb.call(_charge))`, the retry wrapper could continue after the circuit opens and would undermine fast-fail behavior.

## Task 2 - Circuit Breaker and Rate Limiter

### Circuit breaker

I implemented the CLOSED, OPEN, and HALF_OPEN transitions in `CircuitBreaker.call()`:

```python
async def call(self, func):
    if self.state == self.OPEN:
        if time.time() - self.opened_at >= self.cooldown:
            self._transition(self.HALF_OPEN)
        else:
            raise CircuitOpenError(f"circuit[{self.name}] OPEN")

    try:
        result = await func()
        self.failures = 0
        self._transition(self.CLOSED)
        return result
    except Exception:
        self.failures += 1
        self.opened_at = time.time()
        if self.state == self.HALF_OPEN or self.failures >= self.threshold:
            self._transition(self.OPEN)
        raise
```

The local test used a threshold of two and a short cooldown. It opened the circuit after two failures, verified immediate fast-fail, then made a successful recovery call:

```text
circuit breaker:
  state after two failures: OPEN
  fast fail: circuit[local-payments] OPEN
  recovery call: charged
  state after recovery: CLOSED
```

### Rate limiter

I implemented the one-second sliding window using the existing deque for each normalized path:

```python
def allow(self, key: str) -> bool:
    now = time.time()
    q = self.hits[key]
    cutoff = now - self.window_s
    while q and q[0] < cutoff:
        q.popleft()
    if len(q) >= self.rps:
        return False
    q.append(now)
    return True
```

The local test set the limit to three requests per second. The first three requests passed, the next two were rejected, and a request passed again after the window expired. I also exercised the actual FastAPI middleware with a two-request limit:

```text
rate-limit middleware:
  statuses: [404, 404, 429]
  third response Retry-After: 1
rate limiter:
  five-request burst: [True, True, True, False, False]
  request after one second: True
```

The first two middleware requests reached the missing route and returned 404. The third request was rejected before routing with HTTP 429 and included `Retry-After: 1`.

These local checks verified the pattern behavior in one process. I could not collect the five-replica circuit-breaker counts, checkout response split, p99 latency, or Prometheus cluster queries because the Docker image build timed out during setup.

After the checks, I stopped the QuickTicket k3d containers:

```bash
docker ps --format '{{.Names}} {{.Status}}' | rg 'k3d-quickticket' || echo 'quickticket containers: stopped'
```

Output:

```text
quickticket containers: stopped
```

## Bonus Task

I did not complete the Bulkhead Bonus Task.
