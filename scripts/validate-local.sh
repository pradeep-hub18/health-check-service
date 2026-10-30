#!/usr/bin/env bash
# Build and validate a local instance of Health Check Service for reviewer inspection.
# Supported shells: macOS Bash, Linux Bash, and Bash inside WSL2.
# A working Docker Engine is a prerequisite; this script does not install or start Docker.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHART_DIR="$PROJECT_ROOT/helm/health-check-service"

log() { printf '\n[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: bash scripts/validate-local.sh [--cleanup] [--help]

Builds the application image, runs the complete unit/integration suite against
a disposable PostgreSQL container, validates Helm output, then deploys a fresh
Minikube profile and smoke-tests registration, scheduling, history, transitions,
metrics, and PVC retention. On success, the reviewer deployment and sample
endpoint/history remain running by default. Pass --cleanup to remove them.

Requirements:
  - Docker CLI connected to a running Docker Engine (checked with `docker info`)
  - Bash, curl, Python 3, and a supported platform
  - macOS: Homebrew; Linux/WSL2: curl and sha256sum
  - Internet access for container images, package downloads, Python packages,
    and the live https://example.com check
  - Minikube's recommended local resources: 2 CPUs, 2 GiB RAM, and 20 GiB disk

Supported hosts:
  - macOS (Homebrew installs missing kubectl, Helm, and Minikube)
  - Linux x86_64/ARM64, including Ubuntu in WSL2 (verified upstream binaries
    are installed under ~/.local/bin)
  - Native Windows users should run this from WSL2 with Docker Desktop's WSL
    integration enabled. Git Bash/native PowerShell are not supported here.

The script uses unique names, a private reviewer state directory, an isolated
kubeconfig, and a temporary Kubernetes namespace. On success it keeps the
Minikube profile, app deployment, PostgreSQL volume, Secret, and sample
endpoint/history. It always removes its separate test database and network.
On failure, or with --cleanup, it removes the resources it created. It does not
commit to Git, start/stop Docker, or remove shared base images. Missing CLIs may
be installed on the host using Homebrew or ~/.local/bin.
EOF
}

CLEANUP_ON_SUCCESS=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --help|-h)
      usage
      exit 0
      ;;
    --cleanup)
      CLEANUP_ON_SUCCESS=1
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
  shift
done

case "$(uname -s)" in
  Darwin)
    HOST_KIND="macos"
    ;;
  Linux)
    HOST_KIND="linux"
    if [ -r /proc/version ] && grep -qi microsoft /proc/version; then
      HOST_KIND="wsl2"
    fi
    ;;
  MINGW*|MSYS*|CYGWIN*)
    die "Run this Bash script inside WSL2 on Windows; native Git Bash is not supported."
    ;;
  *)
    die "Unsupported operating system: $(uname -s). Use macOS, Linux, or WSL2."
    ;;
esac

for required in docker curl python3; do
  command -v "$required" >/dev/null 2>&1 || die "Required command '$required' is missing."
done

RUN_ID="$(python3 -c 'import uuid; print(uuid.uuid4().hex[:10])')"
RESOURCE_PREFIX="hc-val-$RUN_ID"
PROFILE="$RESOURCE_PREFIX"
KUBE_NAMESPACE="$RESOURCE_PREFIX"
HELM_RELEASE="$RESOURCE_PREFIX"
FULLNAME="health-check-$RUN_ID"
TEST_NETWORK="$RESOURCE_PREFIX-tests"
TEST_DB_CONTAINER="$RESOURCE_PREFIX-db"
APP_IMAGE_REPOSITORY="health-check-service"
APP_IMAGE_TAG="validation-$RUN_ID"
APP_IMAGE="$APP_IMAGE_REPOSITORY:$APP_IMAGE_TAG"

TEMP_ROOT="${TMPDIR:-/tmp}"
TEMP_DIR="$(mktemp -d "${TEMP_ROOT%/}/health-check-validation.XXXXXX")"
STATE_ROOT="${HEALTHCHECK_VALIDATION_HOME:-${HOME:?HOME must be set}/.health-check-service-validation}"
STATE_DIR="$STATE_ROOT/$RUN_ID"
umask 077
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
TEST_NET_CREATED=0
TEST_DB_STARTED=0
APP_IMAGE_BUILT=0
PROFILE_STARTED=0
KUBE_NAMESPACE_CREATED=0
HELM_INSTALLED=0
PORT_FORWARD_PID=""
ENDPOINT_ID=""
BASE_URL=""
SECRET_DIR="$TEMP_DIR/secret"

export MINIKUBE_HOME="$STATE_DIR/minikube-home"
export KUBECONFIG="$STATE_DIR/kubeconfig"
export XDG_CACHE_HOME="$TEMP_DIR/xdg-cache"
export XDG_CONFIG_HOME="$TEMP_DIR/xdg-config"
export XDG_DATA_HOME="$TEMP_DIR/xdg-data"
mkdir -p "$MINIKUBE_HOME" "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME"

cleanup() {
  result=$?
  KEEP_REVIEW_ENV=0
  if [ "$result" -eq 0 ] && [ "$CLEANUP_ON_SUCCESS" -eq 0 ]; then
    KEEP_REVIEW_ENV=1
  fi
  trap - EXIT INT TERM

  if [ "$result" -ne 0 ] && [ "$KUBE_NAMESPACE_CREATED" -eq 1 ]; then
    log "Validation failed; collecting short-lived pod diagnostics before cleanup"
    kubectl --context "$PROFILE" -n "$KUBE_NAMESPACE" get pods,events --sort-by=.metadata.creationTimestamp 2>&1 || true
    kubectl --context "$PROFILE" -n "$KUBE_NAMESPACE" logs "deployment/$FULLNAME" --all-containers --tail=80 2>&1 || true
    kubectl --context "$PROFILE" -n "$KUBE_NAMESPACE" logs "statefulset/$FULLNAME-postgres" --all-containers --tail=80 2>&1 || true
  fi

  if [ -n "$PORT_FORWARD_PID" ]; then
    kill "$PORT_FORWARD_PID" >/dev/null 2>&1 || true
    wait "$PORT_FORWARD_PID" >/dev/null 2>&1 || true
  fi

  if [ "$KEEP_REVIEW_ENV" -eq 0 ] && [ -n "$ENDPOINT_ID" ] && [ -n "$BASE_URL" ]; then
    curl --noproxy '*' -sS --max-time 3 -X DELETE \
      "$BASE_URL/endpoints/$ENDPOINT_ID" >/dev/null 2>&1 || true
  fi

  if [ "$KEEP_REVIEW_ENV" -eq 0 ] && [ "$HELM_INSTALLED" -eq 1 ]; then
    helm --kube-context "$PROFILE" uninstall "$HELM_RELEASE" \
      --namespace "$KUBE_NAMESPACE" --wait >/dev/null 2>&1 || true
  fi
  if [ "$KEEP_REVIEW_ENV" -eq 0 ] && [ "$KUBE_NAMESPACE_CREATED" -eq 1 ]; then
    kubectl --context "$PROFILE" delete namespace "$KUBE_NAMESPACE" \
      --wait=true --timeout=90s >/dev/null 2>&1 || true
  fi
  if [ "$KEEP_REVIEW_ENV" -eq 0 ] && [ "$PROFILE_STARTED" -eq 1 ]; then
    minikube --profile "$PROFILE" delete --purge >/dev/null 2>&1 || \
      minikube --profile "$PROFILE" delete >/dev/null 2>&1 || true
  fi
  if [ "$TEST_DB_STARTED" -eq 1 ]; then
    docker rm --force "$TEST_DB_CONTAINER" >/dev/null 2>&1 || true
  fi
  if [ "$TEST_NET_CREATED" -eq 1 ]; then
    docker network rm "$TEST_NETWORK" >/dev/null 2>&1 || true
  fi
  if [ "$KEEP_REVIEW_ENV" -eq 0 ] && [ "$APP_IMAGE_BUILT" -eq 1 ]; then
    docker image rm "$APP_IMAGE" >/dev/null 2>&1 || \
      printf 'Warning: unable to remove temporary image %s\n' "$APP_IMAGE" >&2
  fi
  rm -rf "$TEMP_DIR"

  if [ "$KEEP_REVIEW_ENV" -eq 1 ]; then
    printf '\nPASS: End-to-end validation succeeded. The Python suite, dependency and vulnerability checks, Docker build, Helm lint/template checks, Minikube rollout, live API/scheduler checks, and PostgreSQL history retention across Helm uninstall/reinstall all passed.\n'
    printf 'The Health Check Service, PostgreSQL, PVC, and https://example.com endpoint/history are still running and available for reviewer inspection.\n'
    printf 'Reviewer Minikube profile: %s\n' "$PROFILE"
    printf 'Reviewer namespace: %s\n' "$KUBE_NAMESPACE"
    printf 'Helm release: %s\n' "$HELM_RELEASE"
    printf 'Sample endpoint ID: %s (https://example.com)\n' "$ENDPOINT_ID"
    printf 'Private kubeconfig: %s\n' "$KUBECONFIG"
    printf 'Persistent Minikube state: %s\n' "$MINIKUBE_HOME"
    printf '\nInspect the deployment:\n'
    printf '  KUBECONFIG="%s" kubectl --context "%s" -n "%s" get pods,services,pvc\n' "$KUBECONFIG" "$PROFILE" "$KUBE_NAMESPACE"
    printf '  KUBECONFIG="%s" kubectl --context "%s" -n "%s" logs deployment/%s --tail=100\n' "$KUBECONFIG" "$PROFILE" "$KUBE_NAMESPACE" "$FULLNAME"
    printf '  KUBECONFIG="%s" kubectl --context "%s" -n "%s" port-forward service/%s 8000:8000\n' "$KUBECONFIG" "$PROFILE" "$KUBE_NAMESPACE" "$FULLNAME"
    printf '  KUBECONFIG="%s" kubectl --context "%s" -n "%s" exec -it %s-postgres-0 -- sh -c '\''psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'\''\n' "$KUBECONFIG" "$PROFILE" "$KUBE_NAMESPACE" "$FULLNAME"
    printf '\nTo remove this reviewer environment later:\n'
    printf '  MINIKUBE_HOME="%s" minikube --profile "%s" delete --purge\n' "$MINIKUBE_HOME" "$PROFILE"
    printf '  rm -rf "%s"\n' "$STATE_DIR"
    printf '\nThe disposable test database/network and temporary files were removed; shared base-image cache was left intact.\n'
  elif [ "$result" -eq 0 ]; then
    log "Validation passed and --cleanup was requested; all temporary test/reviewer resources were removed. Shared base-image cache was left intact."
  else
    printf '\nValidation stopped with exit code %s.\n' "$result" >&2
  fi
  if [ "$KEEP_REVIEW_ENV" -eq 0 ]; then
    rm -rf "$STATE_DIR"
  fi
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

log "Checking that Docker Engine is running and this user can reach it"
if ! docker info >"$TEMP_DIR/docker-info.txt" 2>&1; then
  cat "$TEMP_DIR/docker-info.txt" >&2
  die "Docker Engine is not reachable. Start Docker Desktop/Engine and ensure this user can run 'docker info', then rerun. No containers or cluster resources have been created."
fi

if [ "$HOST_KIND" = "macos" ]; then
  command -v brew >/dev/null 2>&1 || die "Homebrew is required to install missing tools on macOS. Install Homebrew, then rerun."
  missing=""
  for tool in kubectl helm minikube; do
    if ! command -v "$tool" >/dev/null 2>&1; then
      missing="$missing $tool"
    fi
  done
  if [ -n "$missing" ]; then
    log "Installing missing tools with Homebrew:$missing"
    # shellcheck disable=SC2086
    brew install $missing
    eval "$(brew shellenv)"
  fi
elif [ "$HOST_KIND" = "linux" ] || [ "$HOST_KIND" = "wsl2" ]; then
  LOCAL_BIN="${HOME:?HOME must be set}/.local/bin"
  mkdir -p "$LOCAL_BIN"
  export PATH="$LOCAL_BIN:$PATH"

  if ! command -v sha256sum >/dev/null 2>&1; then
    die "sha256sum is required to verify downloaded Linux tools. Install coreutils with your distribution package manager, then rerun."
  fi

  case "$(uname -m)" in
    x86_64|amd64) LINUX_ARCH="amd64" ;;
    aarch64|arm64) LINUX_ARCH="arm64" ;;
    *) die "Linux architecture $(uname -m) is unsupported; expected x86_64 or ARM64." ;;
  esac

  release_asset() {
    repository="$1"
    wanted_asset="$2"
    release_json="$TEMP_DIR/release.json"
    curl --fail --silent --show-error --location \
      "https://api.github.com/repos/$repository/releases/latest" -o "$release_json"
    python3 - "$release_json" "$wanted_asset" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as source:
    release = json.load(source)
asset = next((item for item in release.get("assets", []) if item.get("name") == sys.argv[2]), None)
if asset is None or not asset.get("browser_download_url") or not asset.get("digest", "").startswith("sha256:"):
    raise SystemExit("Latest release lacks the requested asset or a SHA-256 digest: " + sys.argv[2])
print(asset["browser_download_url"])
print(asset["digest"].split(":", 1)[1])
PY
  }

  install_verified_asset() {
    repository="$1"
    asset="$2"
    destination="$3"
    asset_info="$(release_asset "$repository" "$asset")"
    asset_url="$(printf '%s\n' "$asset_info" | sed -n '1p')"
    expected_sha="$(printf '%s\n' "$asset_info" | sed -n '2p')"
    downloaded="$TEMP_DIR/$asset"
    curl --fail --silent --show-error --location "$asset_url" -o "$downloaded"
    actual_sha="$(sha256sum "$downloaded" | awk '{print $1}')"
    [ "$actual_sha" = "$expected_sha" ] || die "SHA-256 verification failed for $asset from $repository."
    install -m 0755 "$downloaded" "$destination"
  }

  if ! command -v kubectl >/dev/null 2>&1; then
    log "Installing verified kubectl under $LOCAL_BIN"
    kubectl_version="$(curl --fail --silent --show-error --location https://dl.k8s.io/release/stable.txt)"
    case "$kubectl_version" in v[0-9]*.[0-9]*.[0-9]*) ;; *) die "Kubernetes returned an invalid stable kubectl version." ;; esac
    curl --fail --silent --show-error --location \
      "https://dl.k8s.io/release/$kubectl_version/bin/linux/$LINUX_ARCH/kubectl" \
      -o "$TEMP_DIR/kubectl"
    expected_sha="$(curl --fail --silent --show-error --location \
      "https://dl.k8s.io/release/$kubectl_version/bin/linux/$LINUX_ARCH/kubectl.sha256")"
    actual_sha="$(sha256sum "$TEMP_DIR/kubectl" | awk '{print $1}')"
    [ "$actual_sha" = "$expected_sha" ] || die "kubectl SHA-256 verification failed."
    install -m 0755 "$TEMP_DIR/kubectl" "$LOCAL_BIN/kubectl"
  fi
  if ! command -v helm >/dev/null 2>&1; then
    log "Installing verified Helm under $LOCAL_BIN"
    HELM_VERSION="$(python3 -c 'import json,urllib.request; print(json.load(urllib.request.urlopen("https://api.github.com/repos/helm/helm/releases/latest"))["tag_name"])')"
    case "$HELM_VERSION" in v[0-9]*.[0-9]*.[0-9]*) ;; *) die "Could not determine a stable Helm release version." ;; esac
    curl --fail --silent --show-error --location \
      "https://raw.githubusercontent.com/helm/helm/$HELM_VERSION/scripts/get-helm-4" \
      -o "$TEMP_DIR/get-helm-4"
    HELM_INSTALL_DIR="$LOCAL_BIN" USE_SUDO=false DESIRED_VERSION="$HELM_VERSION" \
      bash "$TEMP_DIR/get-helm-4"
  fi
  if ! command -v minikube >/dev/null 2>&1; then
    log "Installing verified Minikube under $LOCAL_BIN"
    install_verified_asset "kubernetes/minikube" "minikube-linux-$LINUX_ARCH" "$LOCAL_BIN/minikube"
  fi
else
  die "Unsupported platform."
fi

for required in docker curl python3 kubectl helm minikube; do
  command -v "$required" >/dev/null 2>&1 || die "Required command '$required' is missing after setup."
done

log "Tool versions"
docker version --format 'Docker client={{.Client.Version}} engine={{.Server.Version}}'
python3 --version
kubectl version --client=true
helm version --short
minikube version

log "Checking chart schema, lint, render, and invalid configurations"
helm lint --strict "$CHART_DIR" --set database.existingSecret="$RESOURCE_PREFIX-db"
helm lint --strict "$CHART_DIR" --set database.password=validation-only-dummy
helm template "$HELM_RELEASE" "$CHART_DIR" --namespace "$KUBE_NAMESPACE" \
  --set fullnameOverride="$FULLNAME" \
  --set database.existingSecret="$RESOURCE_PREFIX-db" \
  --set image.repository="$APP_IMAGE_REPOSITORY" --set image.tag="$APP_IMAGE_TAG" \
  --set image.pullPolicy=Never >"$TEMP_DIR/rendered.yaml"
if helm template "$HELM_RELEASE" "$CHART_DIR" --namespace "$KUBE_NAMESPACE" \
  --set database.existingSecret="$RESOURCE_PREFIX-db" --set replicaCount=2 \
  >"$TEMP_DIR/invalid-replicas.yaml" 2>&1; then
  die "Helm unexpectedly accepted replicaCount=2."
fi
if helm template "$HELM_RELEASE" "$CHART_DIR" --namespace "$KUBE_NAMESPACE" \
  --set database.existingSecret="$RESOURCE_PREFIX-db" --set scheduler.maxWorkers=0 \
  >"$TEMP_DIR/invalid-workers.yaml" 2>&1; then
  die "Helm unexpectedly accepted scheduler.maxWorkers=0."
fi
if helm template "$HELM_RELEASE" "$CHART_DIR" --namespace "$KUBE_NAMESPACE" \
  --set database.existingSecret= --set database.password= \
  >"$TEMP_DIR/invalid-secret.yaml" 2>&1; then
  die "Helm unexpectedly accepted missing database credentials."
fi
helm template "$HELM_RELEASE" "$CHART_DIR" --namespace "$KUBE_NAMESPACE" \
  --set fullnameOverride="$FULLNAME" --set database.existingSecret="$RESOURCE_PREFIX-db" \
  --set service.type=NodePort --set service.nodePort=30080 \
  --set database.port=5433 --set database.storageClass=standard \
  >"$TEMP_DIR/rendered-alternate.yaml"
log "Helm rejected the three invalid configurations and rendered default/alternate configurations"

log "Building the application image"
docker build --tag "$APP_IMAGE" "$PROJECT_ROOT"
APP_IMAGE_BUILT=1

log "Preparing a disposable PostgreSQL database for the full Python 3.12 test suite"
docker network create --label "health-check-validation=$RUN_ID" "$TEST_NETWORK" >/dev/null
TEST_NET_CREATED=1
DB_NAME="healthcheck_test"
DB_USER="healthcheck_test"
DB_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
umask 077
printf 'POSTGRES_DB=%s\nPOSTGRES_USER=%s\nPOSTGRES_PASSWORD=%s\n' \
  "$DB_NAME" "$DB_USER" "$DB_PASSWORD" >"$TEMP_DIR/postgres.env"
printf 'TEST_DATABASE_DSN=host=%s port=5432 dbname=%s user=%s password=%s\n' \
  "$TEST_DB_CONTAINER" "$DB_NAME" "$DB_USER" "$DB_PASSWORD" >"$TEMP_DIR/test.env"
docker pull postgres:16
docker run --detach --name "$TEST_DB_CONTAINER" \
  --label "health-check-validation=$RUN_ID" \
  --network "$TEST_NETWORK" --env-file "$TEMP_DIR/postgres.env" postgres:16 >/dev/null
TEST_DB_STARTED=1

attempt=0
until docker exec "$TEST_DB_CONTAINER" pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  [ "$attempt" -lt 60 ] || die "Disposable PostgreSQL did not become ready."
  sleep 2
done

docker pull python:3.12-slim
docker run --rm --network "$TEST_NETWORK" \
  --label "health-check-validation=$RUN_ID" \
  --env-file "$TEMP_DIR/test.env" \
  --mount "type=bind,source=$PROJECT_ROOT,target=/workspace,readonly" \
  --workdir /workspace python:3.12-slim sh -ceu '
    apt-get update
    apt-get install -y --no-install-recommends openssl ca-certificates
    python3 -m pip install --no-cache-dir -r requirements-dev.txt pip-audit
    python3 -m pip check
    python3 -m pytest -q -p no:cacheprovider
    python3 -m pip_audit -r requirements.txt
  '

log "Starting isolated Minikube profile $PROFILE with the Docker driver"
PROFILE_STARTED=1
# The optional preload archive can be very large and is not required for the
# validation: Minikube pulls Kubernetes images on demand, while the app and
# PostgreSQL images are loaded explicitly below.
minikube --profile "$PROFILE" start --driver=docker --preload=false
kubectl --context "$PROFILE" cluster-info >/dev/null
kubectl --context "$PROFILE" get storageclass
kubectl --context "$PROFILE" create namespace "$KUBE_NAMESPACE"
KUBE_NAMESPACE_CREATED=1

mkdir -m 700 "$SECRET_DIR"
printf '%s' "$DB_NAME" >"$SECRET_DIR/POSTGRES_DB"
printf '%s' "$DB_USER" >"$SECRET_DIR/POSTGRES_USER"
printf '%s' "$DB_PASSWORD" >"$SECRET_DIR/POSTGRES_PASSWORD"
kubectl --context "$PROFILE" --namespace "$KUBE_NAMESPACE" \
  create secret generic "$RESOURCE_PREFIX-db" --from-file="$SECRET_DIR"
rm -rf "$SECRET_DIR"

log "Checking rendered resources against the live Kubernetes API (server-side dry-run)"
kubectl --context "$PROFILE" --namespace "$KUBE_NAMESPACE" \
  apply --dry-run=server --validate=strict -f "$TEMP_DIR/rendered.yaml"
kubectl --context "$PROFILE" --namespace "$KUBE_NAMESPACE" \
  apply --dry-run=server --validate=strict -f "$TEMP_DIR/rendered-alternate.yaml"

log "Loading application/database images into the isolated Minikube profile"
docker pull postgres:16
minikube --profile "$PROFILE" image load postgres:16
minikube --profile "$PROFILE" image load "$APP_IMAGE"

log "Installing the application and PostgreSQL into the disposable namespace"
helm --kube-context "$PROFILE" upgrade --install "$HELM_RELEASE" "$CHART_DIR" \
  --namespace "$KUBE_NAMESPACE" --set fullnameOverride="$FULLNAME" \
  --set database.existingSecret="$RESOURCE_PREFIX-db" \
  --set image.repository="$APP_IMAGE_REPOSITORY" --set image.tag="$APP_IMAGE_TAG" \
  --set image.pullPolicy=Never --wait --timeout 5m
HELM_INSTALLED=1
kubectl --context "$PROFILE" --namespace "$KUBE_NAMESPACE" \
  rollout status statefulset/"$FULLNAME-postgres" --timeout=180s
kubectl --context "$PROFILE" --namespace "$KUBE_NAMESPACE" \
  rollout status deployment/"$FULLNAME" --timeout=180s
kubectl --context "$PROFILE" --namespace "$KUBE_NAMESPACE" get pods,pvc
APP_UID="$(kubectl --context "$PROFILE" --namespace "$KUBE_NAMESPACE" \
  exec "deployment/$FULLNAME" -- id -u | tr -d '\r')"
[ "$APP_UID" = "10001" ] || die "Expected app container UID 10001, got '$APP_UID'."

LOCAL_PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
BASE_URL="http://127.0.0.1:$LOCAL_PORT"
kubectl --context "$PROFILE" --namespace "$KUBE_NAMESPACE" \
  port-forward "service/$FULLNAME" "$LOCAL_PORT:8000" >"$TEMP_DIR/port-forward.log" 2>&1 &
PORT_FORWARD_PID=$!

attempt=0
until curl --noproxy '*' --silent --show-error --max-time 2 \
  "$BASE_URL/readyz" -o "$TEMP_DIR/ready.json" 2>/dev/null; do
  attempt=$((attempt + 1))
  [ "$attempt" -lt 60 ] || die "The service did not become reachable through port-forward."
  sleep 1
done
READY_CODE="$(curl --noproxy '*' --silent --show-error --max-time 3 \
  -o "$TEMP_DIR/ready.json" -w '%{http_code}' "$BASE_URL/readyz")"
[ "$READY_CODE" = "200" ] || die "Readiness returned HTTP $READY_CODE."
HEALTH_CODE="$(curl --noproxy '*' --silent --show-error --max-time 3 \
  -o "$TEMP_DIR/health.json" -w '%{http_code}' "$BASE_URL/healthz")"
[ "$HEALTH_CODE" = "200" ] || die "Liveness returned HTTP $HEALTH_CODE."

log "Registering https://example.com and waiting for a healthy scheduled result"
REGISTER_CODE="$(curl --noproxy '*' --silent --show-error --max-time 10 \
  -H 'Content-Type: application/json' -X POST \
  --data '{"url":"https://example.com","interval_seconds":30,"expected_status_code":200}' \
  -o "$TEMP_DIR/register.json" -w '%{http_code}' "$BASE_URL/endpoints")"
[ "$REGISTER_CODE" = "201" ] || {
  cat "$TEMP_DIR/register.json" >&2
  die "Endpoint registration returned HTTP $REGISTER_CODE."
}
ENDPOINT_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$TEMP_DIR/register.json")"
printf '%s\n' "$ENDPOINT_ID" >"$STATE_DIR/endpoint-id"

attempt=0
while :; do
  history_code="$(curl --noproxy '*' --silent --show-error --max-time 5 \
    -o "$TEMP_DIR/history.json" -w '%{http_code}' \
    "$BASE_URL/endpoints/$ENDPOINT_ID/history" || true)"
  if [ "$history_code" = "200" ] && python3 - "$TEMP_DIR/history.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as source:
    data = json.load(source)
history = data.get("history", [])
raise SystemExit(0 if history and history[0].get("success") is True and history[0].get("actual_status_code") == 200 else 1)
PY
  then
    break
  fi
  attempt=$((attempt + 1))
  [ "$attempt" -lt 120 ] || {
    cat "$TEMP_DIR/history.json" >&2 || true
    die "No successful example.com result was persisted within 120 seconds; check cluster DNS/egress."
  }
  sleep 1
done

curl --noproxy '*' --silent --show-error --max-time 5 \
  "$BASE_URL/endpoints/$ENDPOINT_ID/transitions" -o "$TEMP_DIR/transitions.json"
python3 - "$TEMP_DIR/transitions.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as source:
    data = json.load(source)
transitions = data.get("transitions", [])
if not any(row.get("from_state") == "unknown" and row.get("to_state") == "healthy" for row in transitions):
    raise SystemExit("Expected unknown-to-healthy state transition was not recorded.")
PY
curl --noproxy '*' --silent --show-error --max-time 5 "$BASE_URL/metrics" \
  -o "$TEMP_DIR/metrics.txt"
grep -q 'health_checks_total' "$TEMP_DIR/metrics.txt" || die "Prometheus check metric was not found."

log "Verifying the PostgreSQL claim survives Helm uninstall, then restoring the release for review"
if [ -n "$PORT_FORWARD_PID" ]; then
  kill "$PORT_FORWARD_PID" >/dev/null 2>&1 || true
  wait "$PORT_FORWARD_PID" >/dev/null 2>&1 || true
  PORT_FORWARD_PID=""
fi
helm --kube-context "$PROFILE" uninstall "$HELM_RELEASE" \
  --namespace "$KUBE_NAMESPACE" --wait
HELM_INSTALLED=0
PVC_NAME="postgres-data-$FULLNAME-postgres-0"
kubectl --context "$PROFILE" --namespace "$KUBE_NAMESPACE" \
  get pvc "$PVC_NAME" -o jsonpath='{.status.phase}' | grep -qx Bound || \
  die "PostgreSQL PVC was not Bound after Helm uninstall."

log "Reinstalling the app against its retained PostgreSQL volume"
HELM_INSTALLED=1
helm --kube-context "$PROFILE" upgrade --install "$HELM_RELEASE" "$CHART_DIR" \
  --namespace "$KUBE_NAMESPACE" --set fullnameOverride="$FULLNAME" \
  --set database.existingSecret="$RESOURCE_PREFIX-db" \
  --set image.repository="$APP_IMAGE_REPOSITORY" --set image.tag="$APP_IMAGE_TAG" \
  --set image.pullPolicy=Never --wait --timeout 5m
kubectl --context "$PROFILE" --namespace "$KUBE_NAMESPACE" \
  rollout status statefulset/"$FULLNAME-postgres" --timeout=180s
kubectl --context "$PROFILE" --namespace "$KUBE_NAMESPACE" \
  rollout status deployment/"$FULLNAME" --timeout=180s
kubectl --context "$PROFILE" --namespace "$KUBE_NAMESPACE" get pods,pvc

kubectl --context "$PROFILE" --namespace "$KUBE_NAMESPACE" \
  port-forward "service/$FULLNAME" "$LOCAL_PORT:8000" >"$TEMP_DIR/port-forward-after-restore.log" 2>&1 &
PORT_FORWARD_PID=$!
attempt=0
until curl --noproxy '*' --silent --show-error --max-time 2 \
  "$BASE_URL/readyz" -o "$TEMP_DIR/ready-after-restore.json" 2>/dev/null; do
  attempt=$((attempt + 1))
  [ "$attempt" -lt 60 ] || die "The restored service did not become reachable through port-forward."
  sleep 1
done
PERSISTED_HISTORY_CODE="$(curl --noproxy '*' --silent --show-error --max-time 5 \
  -o "$TEMP_DIR/persisted-history.json" -w '%{http_code}' \
  "$BASE_URL/endpoints/$ENDPOINT_ID/history")"
[ "$PERSISTED_HISTORY_CODE" = "200" ] || die "History was not available after reinstall; HTTP $PERSISTED_HISTORY_CODE."
python3 - "$TEMP_DIR/persisted-history.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as source:
    data = json.load(source)
history = data.get("history", [])
if not history or history[0].get("success") is not True:
    raise SystemExit("The successful check history did not survive Helm uninstall/reinstall.")
PY
READY_AFTER_RESTORE="$(curl --noproxy '*' --silent --show-error --max-time 3 \
  -o "$TEMP_DIR/ready-after-restore.json" -w '%{http_code}' "$BASE_URL/readyz")"
[ "$READY_AFTER_RESTORE" = "200" ] || die "Readiness after reinstall returned HTTP $READY_AFTER_RESTORE."

log "PASS: full Python suite/audit, Docker build, Helm validation, Minikube install, live API/scheduler smoke, data retention, and post-restore readiness"
log "The successful https://example.com endpoint, check history, and healthy transition are retained for review."
