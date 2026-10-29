# Submission validation and change report

Validated on **29 October 2026 (Asia/Kolkata)**. The project directory is `health-check-service`. Every project source, test, configuration, chart and documentation file was inspected before editing. At the start of validation, the Git repository had no commits and all project files were untracked. Publishing the completed project is a separate step after this validation snapshot.

## Readiness

**Ready for submission as the documented single-instance take-home MVP. No outstanding implementation or validation blockers were found.** The existing Flask API, PostgreSQL tables, concurrent scheduler, metrics, Docker workflow and Helm chart are preserved and improved. The three-region design remains an explicit proposal; no 50,000-endpoint load claim is made.

Existing installations of the original chart require the documented backup/restore migration because resource names, selectors and PVC identity changed. Do not blindly upgrade an existing data-bearing release.

## Changes

- Fixed the scheduler's synchronous-future callback deadlock, bounded unfinished work to the worker count, added fair overdue dispatch, prevented overlapping checks, removed stale bookkeeping, and tested recovery after database/submission/check failures.
- Added import-safe startup, SIGINT/SIGTERM handling, graceful draining, a final process shutdown deadline, and validation of non-finite timeout values. A real active HTTP check completed and committed during SIGTERM shutdown.
- Added strict URL/body/integer/timestamp validation, including malformed Unicode; distinguished invalid history filters (400) from database outages (503).
- Added destination policy and DNS/IP pinning; preserved HTTPS SNI/Host/certificate verification; blocked redirects, inherited proxies/netrc credentials, body buffering and private addresses by default. Private monitoring remains an explicit trusted-operator opt-in.
- Preserved atomic result/current-state/transition transactions and cascading deletion. Added deterministic coverage of failure rollback and deletion during an active check.
- Removed embedded database passwords, bounded connection/statement/lock waits, kept metrics available during database failures, added database/scheduler metrics, and included scheduler health in runner readiness.
- Completed README, detailed three-region design, and operational runbook with alerts, troubleshooting, credentials, backups/restores, retention, deployment and migration procedures.
- Removed unused dependencies, separated test requirements, updated vulnerable HTTP dependencies and the image's pip, and made the image non-root with a limited build context.
- Corrected Helm naming/selectors/ports/probes, enforced one app replica and Recreate updates, supported external Secrets, added schema validation, configured app/database/init resources and security contexts, and retained PostgreSQL PVCs.

## Executed validation

| Check | Actual result |
| --- | --- |
| Final Python 3.12 container suite, with real disposable PostgreSQL and local HTTP/TLS fixtures | **147 passed, 0 skipped** in 3.54 seconds: 139 unit tests and 8 integration tests |
| Dependency consistency (`python3 -m pip check`) | Passed: no broken requirements |
| Final Docker build | Passed for Linux ARM64 on Docker Desktop |
| Final image Python-package audit (`pip-audit`) | **17 installed packages audited; zero known advisories reported** |
| Strict Helm lint, external-Secret variant | Passed |
| Strict Helm lint, generated-Secret variant with dummy credential | Passed |
| Helm render, standard and alternate NodePort/custom-port/storage-class variants | Passed |
| Kubernetes client dry-run with strict validation of both rendered variants | Passed against the local Minikube API schema |
| Invalid Helm configurations: missing credentials, replica count 2, worker count 0 | Correctly rejected |
| Manifest selectors, ports, resources, security contexts and retention inspection | Passed |
| Real Minikube install and rollout of the final security-updated image | App and PostgreSQL ready, zero restarts; PVC Bound; readiness 200; updated dependency versions and app UID 10001 verified |
| Earlier full Minikube smoke | Root/liveness/readiness/metrics/list API checks passed; private-address registration rejected; read-only root and PostgreSQL UID 999 verified |
| PVC retention | Claim remained Bound after Helm uninstall; only the temporary namespace/PVC was then removed |
| Docker runtime smoke | Non-root UID 10001, read-only root, registration, scheduled history, metrics, SIGTERM exit 0 verified |
| SIGTERM with an active three-second check, final image | Exit 0 after 3.08 seconds; active result committed exactly once |
| Real local HTTPS | Correct SNI/Host/CA succeeded; wrong hostname and untrusted CA failed before HTTP traffic |
| Documentation | All 20 shell examples passed `sh -n`; local Markdown links resolve |

Final image tag: `temporal-healthcheck-validation:local`.
Final Docker manifest-list digest: `sha256:568e30f19fd1dcc9be4e90065aac2e3780017a223e3798319a1a58fddc491b96`.
Base image observed during build: `python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159`.

### Dependency audit and remediation

The initial audit reported advisories in Requests 2.32.5, urllib3 2.6.3 and base-image pip 25.0.1. The returned 21 entries included duplicate advisory records. These were remedied by pinning Requests **2.34.2**, urllib3 **2.8.0**, and Docker build pip **26.2.2**. Published versions were checked before rebuilding. The full suite and audit were rerun against the resulting image; no known Python-package vulnerabilities were reported by that audit. This is not a container operating-system scan or a guarantee against unknown vulnerabilities.

Upstream release evidence: [Requests releases](https://github.com/psf/requests/releases), [urllib3 2.8.0 release and security fixes](https://github.com/urllib3/urllib3/releases/tag/2.8.0). Package availability was also verified using `python3 -m pip index versions requests`, `urllib3`, and `pip` in a disposable container.

## Commands executed

Commands are grouped below; generated credential values are intentionally omitted. Validation ran from a staging copy under the task's `work/project` directory before being applied to the original repository. All Python invocations used `python3`.

### Inspection and environment

- `pwd`, `ls -la`, `rg --files --hidden` with virtual environment/Git/cache exclusions, and `cat` of every project file.
- Checked applicable `AGENTS.md` locations; none were present.
- `git status --short`; `git log -5 --oneline` reported that `main` had no commits.
- `command -v python3 helm docker minikube kubectl psql postgres pg_ctl git` and `python3 --version`.
- Existing virtual environment: `python3 --version` and `python3 -m pip list`.
- `docker version`, `docker ps --format '{{.Names}} {{.Image}} {{.Status}}'`.
- `helm version --short`, `kubectl version --client`, `minikube status`, and Kubernetes current-context/namespace checks.
- `rsync` copied source to a staging directory and preserved a before-change snapshot; hashes were checked to confirm the original had not changed during work.

### Unit/integration and Docker

An isolated `postgres:16` container named `temporal-healthcheck-validation-db` was started using a generated private environment file and a randomly mapped loopback port. It did not use the existing `health-check-postgres` container or its data. Each PostgreSQL integration test created and dropped its own uniquely named schema.

```sh
python3 -m pytest -q -m 'not integration'
python3 -m pytest tests/test_checker.py tests/test_url_policy.py -q
python3 -m pytest -q

docker run --detach --name temporal-healthcheck-validation-db \
  --label codex.task=temporal-healthcheck-validation \
  --env-file work/postgres-test.env -p 127.0.0.1::5432 postgres:16
docker port temporal-healthcheck-validation-db 5432/tcp
docker exec temporal-healthcheck-validation-db pg_isready -U healthcheck_test -d healthcheck_test
docker network create --label codex.task=temporal-healthcheck-validation temporal-healthcheck-validation
docker network connect temporal-healthcheck-validation temporal-healthcheck-validation-db

docker build --tag temporal-healthcheck-validation:local .
```

The final complete test command used the built image, a read-only bind mount of the revised repository at `/tests`, the isolated network, and a private `TEST_DATABASE_DSN` environment file:

```sh
docker run --rm --name temporal-healthcheck-validation-tests \
  --network temporal-healthcheck-validation --env-file work/integration-test.env \
  --mount type=bind,source=ABSOLUTE_STAGING_PROJECT_PATH,target=/tests,readonly \
  --workdir /tests temporal-healthcheck-validation:local sh -c \
  'python3 -m pip install --quiet --no-cache-dir --target /tmp/test-deps pytest==8.4.2 && PYTHONPATH=/tmp/test-deps:/tests python3 -m pytest -q -p no:cacheprovider && python3 -m pip check'
```

`ABSOLUTE_STAGING_PROJECT_PATH` above abbreviates the actual task-local staging path, not an executed literal. Test tools were installed only into the disposable container's `/tmp` directory.

```sh
docker run --rm --name temporal-healthcheck-validation-audit \
  temporal-healthcheck-validation:local sh -c \
  'python3 -m pip install --quiet --no-cache-dir --target /tmp/audit-deps pip-audit && PYTHONPATH=/tmp/audit-deps python3 -m pip_audit --path /usr/local/lib/python3.12/site-packages --cache-dir /tmp/pip-audit-cache --format json'
```

Task-local `python3 work/docker_smoke.py` and `python3 work/shutdown_smoke.py` orchestrated disposable Docker app/HTTP-target containers, exercised the API, sent `docker kill --signal SIGTERM`, checked `docker wait`/logs, verified persisted data with `psql`, and removed their containers. They are validation harnesses, not application components. The permanent regression tests are under `tests/`.

### Helm and Kubernetes

```sh
helm lint helm/health-check-service --strict --set database.existingSecret=healthcheck-db
helm lint helm/health-check-service --strict --set database.password=validation-only-placeholder
helm template healthcheck helm/health-check-service \
  --set database.existingSecret=healthcheck-db > ../helm-rendered.yaml
helm template sample helm/health-check-service \
  --set database.password=validation-only-placeholder \
  --set service.type=NodePort --set service.nodePort=30080 \
  --set database.port=5433 --set database.storageClass=standard \
  > ../helm-rendered-alternate.yaml
kubectl apply --dry-run=client --validate=strict -f ../helm-rendered.yaml
kubectl apply --dry-run=client --validate=strict -f ../helm-rendered-alternate.yaml
```

A Python3/Ruby YAML inspection checked selectors, ports, resource settings and PVC retention; negative Helm configurations were rendered to confirm rejection. Neither dry-run changed live resources.

The deployment smoke used new namespaces `temporal-healthcheck-validation` and, after dependency remediation, `temporal-healthcheck-final-validation`. Both were removed afterward. The final sequence was:

```sh
minikube -p minikube image load temporal-healthcheck-validation:local
kubectl --context minikube create namespace temporal-healthcheck-final-validation
kubectl --context minikube -n temporal-healthcheck-final-validation create secret generic validation-db \
  --from-env-file=work/postgres-test.env
helm install temporal-healthcheck-final-validation work/project/helm/health-check-service \
  --kube-context minikube -n temporal-healthcheck-final-validation \
  --set fullnameOverride=health-check-service --set database.existingSecret=validation-db \
  --set image.repository=temporal-healthcheck-validation --set image.tag=local \
  --set image.pullPolicy=Never --wait --timeout 3m
kubectl --context minikube -n temporal-healthcheck-final-validation rollout status deployment/health-check-service --timeout=45s
kubectl --context minikube -n temporal-healthcheck-final-validation rollout status statefulset/health-check-service-postgres --timeout=45s
kubectl --context minikube -n temporal-healthcheck-final-validation get pods,pvc
helm uninstall temporal-healthcheck-final-validation --kube-context minikube \
  -n temporal-healthcheck-final-validation --wait --timeout 45s
kubectl --context minikube delete namespace temporal-healthcheck-final-validation --wait=true --timeout=45s
```

`kubectl exec ... -- python3 -c ...` additionally asserted UID 10001, Requests/urllib3 versions, and HTTP readiness. Earlier smoke verified all basic GET routes, metrics, private-address rejection and PostgreSQL UID, then explicitly confirmed the PVC survived uninstall before deleting the temporary namespace.

### Failures encountered and resolved

- Initial Docker/Minikube access was blocked by the filesystem sandbox. Authorized tool escalation succeeded; validation was then performed.
- A test harness initially used an incorrect temporary credentials path; correcting that path allowed all PostgreSQL tests to run.
- Docker smoke startup initially encountered a connection reset before the app was listening; the harness was fixed to retry startup. Its log assertion initially captured stdout instead of Docker's stderr; capture was corrected. The app had already exited successfully, and the complete smoke subsequently passed.
- The first dependency-audit invocation could not create a cache under the non-root user's absent home directory; an explicit `/tmp` audit cache fixed it.
- The successful initial audit found vulnerable package versions; dependencies were upgraded and the build, complete suite, audit, active-check shutdown and final Minikube rollout were rerun.
- Host Python 3.9 emitted urllib3's LibreSSL warning. Final dependency/runtime validation used Python 3.12/OpenSSL in Docker, as recommended by the README; the user's existing virtual environment was not modified.

## Remaining limits and unexecuted validation

- The MVP uses a single process and Werkzeug's threaded server. It has no authentication, tenant isolation, distributed schedule ownership, durable backlog, automatic retention, database HA, or built-in backup service. These are explicitly documented production gaps.
- The demo app shares PostgreSQL's bootstrap role; production needs separate administrative/migration/runtime roles, authorization and network policy.
- DNS and HTTP inactivity limits do not establish a total probe deadline; the process drain deadline is the final shutdown bound. No distributed or 50,000-endpoint load/soak test was performed.
- Backup/restore, retention and secret-rotation examples were reviewed and shell-syntax-checked, not executed against the user's database.
- Validation covered Linux ARM64 Docker and the available Minikube cluster, not every architecture/Kubernetes release or registry environment. No container OS vulnerability scanner was run.
- Runtime dependencies are directly pinned, but transitive packages/base image tags may change on future builds; re-audit rebuilt images before production deployment.
- The checks above describe the validated source before public publication; publishing it does not imply an external take-home submission has been made.

## Modified and added files

35 source/documentation/configuration files were modified or added; no project source files were deleted.

- `.dockerignore`
- `.gitignore`
- `Dockerfile`
- `README.md`
- `app/__init__.py`
- `app/checker.py`
- `app/database.py`
- `app/metrics.py`
- `app/routes.py`
- `app/scheduler.py`
- `app/url_policy.py`
- `docs/design.md`
- `docs/runbook.md`
- `docs/validation.md`
- `helm/health-check-service/Chart.yaml`
- `helm/health-check-service/templates/_helpers.tpl`
- `helm/health-check-service/templates/app-deployment.yaml`
- `helm/health-check-service/templates/app-service.yaml`
- `helm/health-check-service/templates/postgres-secret.yaml`
- `helm/health-check-service/templates/postgres-service.yaml`
- `helm/health-check-service/templates/postgres-statefulset.yaml`
- `helm/health-check-service/values.schema.json`
- `helm/health-check-service/values.yaml`
- `pytest.ini`
- `requirements-dev.txt`
- `requirements.txt`
- `run.py`
- `tests/conftest.py`
- `tests/integration/conftest.py`
- `tests/integration/test_postgres.py`
- `tests/test_api.py`
- `tests/test_checker.py`
- `tests/test_runtime.py`
- `tests/test_scheduler.py`
- `tests/test_url_policy.py`

## Cleanup and delivery

Temporary Docker test/app/target/database containers, the validation network, both temporary Kubernetes namespaces and their Secrets/PVCs, and private generated credential files were removed. The completed Docker image remains available locally. Existing Docker/database workloads and existing Minikube workloads were preserved. The validated files were applied to the project repository while preserving its existing virtual environment. This report accompanies the source as submission evidence.
