# Lab 5 - CI/CD and GitOps

## Task 1 - CI Pipeline and ArgoCD Setup

### CI workflow

Created `.github/workflows/ci.yml` for QuickTicket.

The workflow:

- runs on pushes to `main` and `feature/lab5`;
- skips CI-created commits whose message starts with `ci:` to avoid an infinite loop;
- logs in to GitHub Container Registry;
- builds and pushes `gateway`, `events`, and `payments` images to `ghcr.io/n1qro`;
- tags each image with `${{ github.sha }}`;
- updates the raw Kubernetes manifests with the new image tags;
- commits the manifest update back to the same branch.

Workflow run:

```text
https://github.com/N1qro/SRE-Intro/actions/runs/28013129544
```

The successful CI-created manifest update commit was:

```text
1da7ad2 ci: update image tags to cc09beb5f353961ad19dee2b15ce00d33d29b6f9
```

### GHCR images

The workflow pushed the three QuickTicket service images:

```text
ghcr.io/n1qro/quickticket-gateway:cc09beb5f353961ad19dee2b15ce00d33d29b6f9
ghcr.io/n1qro/quickticket-events:cc09beb5f353961ad19dee2b15ce00d33d29b6f9
ghcr.io/n1qro/quickticket-payments:cc09beb5f353961ad19dee2b15ce00d33d29b6f9
```

They were also pullable locally:

```text
Status: Downloaded newer image for ghcr.io/n1qro/quickticket-gateway:cc09beb5f353961ad19dee2b15ce00d33d29b6f9
Status: Downloaded newer image for ghcr.io/n1qro/quickticket-events:cc09beb5f353961ad19dee2b15ce00d33d29b6f9
Status: Downloaded newer image for ghcr.io/n1qro/quickticket-payments:cc09beb5f353961ad19dee2b15ce00d33d29b6f9
```

Package names verified by the pushed image references and successful pulls:

```text
quickticket-events
quickticket-gateway
quickticket-payments
```

### Kubernetes manifests

Updated the raw manifests in `k8s/` so the three application services use GHCR images instead of local-only k3d images:

- `k8s/gateway.yaml`
- `k8s/events.yaml`
- `k8s/payments.yaml`

Each service now uses:

- `image: ghcr.io/n1qro/quickticket-<service>:cc09beb5f353961ad19dee2b15ce00d33d29b6f9`
- `imagePullPolicy: Always`
- `imagePullSecrets` with `ghcr-secret`

The gateway Deployment also has the label `version: "v2"` for GitOps sync verification.

After applying the current manifests locally, all three service Deployments were available with the registry images:

```text
NAME       READY   UP-TO-DATE   AVAILABLE   AGE   CONTAINERS   IMAGES                                                                        SELECTOR
gateway    1/1     1            1           7d    gateway      ghcr.io/n1qro/quickticket-gateway:cc09beb5f353961ad19dee2b15ce00d33d29b6f9    app=gateway
events     1/1     1            1           7d    events       ghcr.io/n1qro/quickticket-events:cc09beb5f353961ad19dee2b15ce00d33d29b6f9     app=events
payments   1/1     1            1           7d    payments     ghcr.io/n1qro/quickticket-payments:cc09beb5f353961ad19dee2b15ce00d33d29b6f9   app=payments
```

The gateway GitOps verification label was present in the cluster:

```text
$ kubectl get deployment gateway -o jsonpath='{.metadata.labels.version}'
v2
```

### ArgoCD installation

Installed ArgoCD in the `argocd` namespace.

The standard install manifest initially hit a large-annotation CRD apply issue, so the install was completed with server-side apply:

```bash
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
kubectl apply --server-side -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
kubectl wait --for=condition=Available deployment/argocd-server -n argocd --timeout=180s
```

ArgoCD pods became ready:

```text
NAME                                                READY   STATUS    RESTARTS   AGE
argocd-application-controller-0                     1/1     Running   0          9m31s
argocd-applicationset-controller-5b887868c5-7btlr   1/1     Running   0          9m43s
argocd-dex-server-64d89d46c8-7dhvc                  1/1     Running   0          9m43s
argocd-notifications-controller-97b9678-7kc24       1/1     Running   0          9m43s
argocd-redis-7c89f9f856-m4tsh                       1/1     Running   0          9m43s
argocd-repo-server-d79cf4d54-wgjn7                  1/1     Running   0          3m10s
argocd-server-54569dc877-fdt47                      1/1     Running   0          9m40s
```

Created the ArgoCD Application declaratively:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: quickticket
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/N1qro/SRE-Intro.git
    targetRevision: feature/lab5
    path: k8s
  destination:
    server: https://kubernetes.default.svc
    namespace: default
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
```

ArgoCD application status:

```text
Name:               argocd/quickticket
Project:            default
Server:             https://kubernetes.default.svc
Namespace:          default
URL:                http://localhost:44263/applications/quickticket
Source:
- Repo:             https://github.com/N1qro/SRE-Intro.git
  Target:           feature/lab5
  Path:             k8s
SyncWindow:         Sync Allowed
Sync Policy:        Automated (Prune)
Sync Status:        Synced to feature/lab5 (1da7ad2)
Health Status:      Healthy

GROUP  KIND        NAMESPACE  NAME      STATUS  HEALTH   HOOK  MESSAGE
       Service     default    payments  Synced  Healthy        service/payments configured
       Service     default    gateway   Synced  Healthy        service/gateway configured
       Service     default    events    Synced  Healthy        service/events configured
       Service     default    redis     Synced  Healthy        service/redis configured
       Service     default    postgres  Synced  Healthy        service/postgres configured
apps   Deployment  default    events    Synced  Healthy        deployment.apps/events configured
apps   Deployment  default    gateway   Synced  Healthy        deployment.apps/gateway configured
apps   Deployment  default    postgres  Synced  Healthy        deployment.apps/postgres configured
apps   Deployment  default    payments  Synced  Healthy        deployment.apps/payments configured
apps   Deployment  default    redis     Synced  Healthy        deployment.apps/redis configured
```

### Manual kubectl edit behavior

If someone manually runs `kubectl edit` on a resource managed by ArgoCD, the live cluster state drifts from the desired state stored in Git. ArgoCD detects the difference and marks the application as `OutOfSync`. Because this application is configured with automated sync and self-heal, ArgoCD restores the resource back to the version declared in Git.

## Task 2 - Rollback via GitOps

### Bad deploy

Changed the gateway image to a non-existent GHCR repository and tag:

```diff
-          image: ghcr.io/n1qro/quickticket-gateway:cc09beb5f353961ad19dee2b15ce00d33d29b6f9
+          image: ghcr.io/n1qro/quickticket-gateway-broken:does-not-exist
```

Committed and pushed the broken deploy while skipping CI, so the bonus image-tag workflow would not automatically repair the manifest before ArgoCD observed it:

```bash
git add k8s/gateway.yaml
git commit -m "feat: deploy broken gateway image [skip ci]"
git push origin feature/lab5
```

After ArgoCD synced the bad commit, the application was no longer healthy:

```text
NAME          SYNC STATUS   HEALTH STATUS   REVISION                                   PROJECT
quickticket   Synced        Progressing     7fb38042fbf541bb051821b39fa57868b9604e38   default
```

The new gateway pod could not pull the broken image:

```text
NAME                                                     READY   STATUS             RESTARTS       AGE
events-67975ff566-8ndwc                                  1/1     Running            0              39m
gateway-79fd9d555c-kcxls                                 1/1     Running            0              39m
gateway-896688f8c-2mtpm                                  0/1     ErrImagePull       0              8s
payments-78f7f4bc55-vjzgh                                1/1     Running            0              39m
postgres-85ffd4fb9f-2d2q7                                1/1     Running            4              7d
redis-6d65768944-s4p8b                                   1/1     Running            4              7d
```

### Git revert rollback

Rolled back through Git, not by editing the live cluster:

```bash
git revert --no-commit HEAD
git commit -m "revert: rollback broken gateway image [skip ci]"
git push origin feature/lab5
```

Git history shows the bad deploy and rollback commits:

```text
af94e3b revert: rollback broken gateway image [skip ci]
7fb3804 feat: deploy broken gateway image [skip ci]
1da7ad2 ci: update image tags to cc09beb5f353961ad19dee2b15ce00d33d29b6f9
cc09beb fix: gitignore was interfering with ci
466bb4b feat(lab5): add CI/CD GitOps pipeline
```

After the rollback commit was synced, ArgoCD returned to Synced and Healthy:

```text
Name:               argocd/quickticket
Project:            default
Server:             https://kubernetes.default.svc
Namespace:          default
URL:                http://localhost:35383/applications/quickticket
Source:
- Repo:             https://github.com/N1qro/SRE-Intro.git
  Target:           feature/lab5
  Path:             k8s
SyncWindow:         Sync Allowed
Sync Policy:        Automated (Prune)
Sync Status:        Synced to feature/lab5 (af94e3b)
Health Status:      Healthy

GROUP  KIND        NAMESPACE  NAME      STATUS  HEALTH   HOOK  MESSAGE
       Service     default    postgres  Synced  Healthy        service/postgres unchanged
       Service     default    gateway   Synced  Healthy        service/gateway unchanged
       Service     default    payments  Synced  Healthy        service/payments unchanged
       Service     default    redis     Synced  Healthy        service/redis unchanged
       Service     default    events    Synced  Healthy        service/events unchanged
apps   Deployment  default    postgres  Synced  Healthy        deployment.apps/postgres unchanged
apps   Deployment  default    payments  Synced  Healthy        deployment.apps/payments unchanged
apps   Deployment  default    events    Synced  Healthy        deployment.apps/events unchanged
apps   Deployment  default    redis     Synced  Healthy        deployment.apps/redis unchanged
apps   Deployment  default    gateway   Synced  Healthy        deployment.apps/gateway configured
```

Gateway returned to the valid GHCR image:

```text
NAME      READY   UP-TO-DATE   AVAILABLE   AGE   CONTAINERS   IMAGES                                                                       SELECTOR
gateway   1/1     1            1           7d    gateway      ghcr.io/n1qro/quickticket-gateway:cc09beb5f353961ad19dee2b15ce00d33d29b6f9   app=gateway
```

Pods after rollback:

```text
NAME                                                     READY   STATUS             RESTARTS       AGE
events-67975ff566-8ndwc                                  1/1     Running            0              50m
gateway-79fd9d555c-kcxls                                 1/1     Running            0              50m
payments-78f7f4bc55-vjzgh                                1/1     Running            0              50m
postgres-85ffd4fb9f-2d2q7                                1/1     Running            4              7d
redis-6d65768944-s4p8b                                   1/1     Running            4              7d
```

Recovery time was about 2-3 minutes from pushing the revert to observing healthy pods in this run. The ArgoCD operation history shows the rollback sync itself took about 2 seconds once ArgoCD picked up the rollback commit:

```text
deployStartedAt: 2026-06-26T17:43:02Z
deployedAt:      2026-06-26T17:43:04Z
revision:        af94e3b16001e2178f1cc211a4e8b168f016f0bf
message:         successfully synced (all tasks run)
```

## Bonus Task - Automated Image Tag Update

The bonus workflow is implemented in `.github/workflows/ci.yml`.

The full loop implemented by the workflow is:

1. A push triggers CI.
2. CI builds and pushes the three service images with the commit SHA tag.
3. CI updates the image tags in `k8s/gateway.yaml`, `k8s/events.yaml`, and `k8s/payments.yaml`.
4. CI commits the manifest update back to the same branch.
5. CI skips that `ci:` commit to avoid an infinite loop.
6. ArgoCD detects and syncs the updated image tag from Git.

Git history showing the code commit and CI tag-update commit:

```text
1da7ad2 ci: update image tags to cc09beb5f353961ad19dee2b15ce00d33d29b6f9
cc09beb fix: gitignore was interfering with ci
466bb4b feat(lab5): add CI/CD GitOps pipeline
```
