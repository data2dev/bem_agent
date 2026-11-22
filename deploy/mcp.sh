#!/usr/bin/env bash
# Manage access to the deployed MCP server. Run it yourself in Cloud Shell.
#
#   bash deploy/mcp.sh init [NAME...]   create the API-key secret (default: owner grok-connector)
#   bash deploy/mcp.sh add NAME         add a key for one person or client; prints it once
#   bash deploy/mcp.sh remove NAME      revoke that key
#   bash deploy/mcp.sh list             key names (never the keys themselves)
#   bash deploy/mcp.sh open             once, after the first deploy: let requests reach the
#                                       server, which then checks the API key itself
#   bash deploy/mcp.sh url              the URL to give an MCP connector
#
# Keys live in Secret Manager as BEM_MCP_KEYS = {"name": "key", ...}. Every change
# creates a new secret version, disables the old ones (so a revoked key can't come back
# from history) and rolls the service so it loads the new keys.

set -euo pipefail

PROJECT_ID="projectbem"
REGION="us-east1"
SERVICE="bem-mcp"
SECRET="BEM_MCP_KEYS"
RUNTIME_SA="bem-runtime@${PROJECT_ID}.iam.gserviceaccount.com"
P=(--project="$PROJECT_ID")

service_exists() { gcloud run services describe "$SERVICE" --region "$REGION" "${P[@]}" >/dev/null 2>&1; }
read_keys() { gcloud secrets versions access latest --secret="$SECRET" "${P[@]}"; }
new_key() { python3 -c 'import secrets; print(secrets.token_urlsafe(32))'; }

valid_name() {
  [[ "$1" =~ ^[A-Za-z0-9._-]{1,40}$ ]] || { echo "Name must be 1-40 letters, digits, . _ -"; exit 1; }
}

write_keys() {  # stdin: JSON
  printf '%s' "$(cat)" | gcloud secrets versions add "$SECRET" --data-file=- "${P[@]}" >/dev/null
  latest="$(gcloud secrets versions list "$SECRET" "${P[@]}" --filter='state=ENABLED' \
    --sort-by='~createTime' --format='value(name)' | head -1)"
  for v in $(gcloud secrets versions list "$SECRET" "${P[@]}" --filter='state=ENABLED' --format='value(name)'); do
    [[ "$v" == "$latest" ]] || gcloud secrets versions disable "$v" --secret="$SECRET" "${P[@]}" --quiet >/dev/null
  done
  if service_exists; then
    echo "Restarting $SERVICE so it loads the new keys..."
    gcloud run services update "$SERVICE" --region "$REGION" "${P[@]}" \
      --update-env-vars "BEM_KEYS_VERSION=$latest" --quiet >/dev/null
  fi
}

print_key() {
  echo
  echo "  $1: $2"
  echo
  echo "Store it now (e.g. in a password manager); it is not shown again."
  echo "Connector header:  Authorization: Bearer $2"
}

cmd="${1:-}"; shift || true
case "$cmd" in
  init)
    if gcloud secrets describe "$SECRET" "${P[@]}" >/dev/null 2>&1; then
      echo "$SECRET already exists. Use: add NAME / remove NAME / list"; exit 1
    fi
    names=("$@"); [[ ${#names[@]} -gt 0 ]] || names=(owner grok-connector)
    json="{}"
    declare -A made
    for n in "${names[@]}"; do
      valid_name "$n"
      k="$(new_key)"; made[$n]="$k"
      json="$(python3 -c 'import json,sys; d=json.loads(sys.argv[1]); d[sys.argv[2]]=sys.argv[3]; print(json.dumps(d))' "$json" "$n" "$k")"
    done
    printf '%s' "$json" | gcloud secrets create "$SECRET" "${P[@]}" --replication-policy=automatic --data-file=- >/dev/null
    gcloud secrets add-iam-policy-binding "$SECRET" "${P[@]}" \
      --member="serviceAccount:${RUNTIME_SA}" --role="roles/secretmanager.secretAccessor" >/dev/null
    echo "Created $SECRET with keys for: ${names[*]}"
    for n in "${names[@]}"; do print_key "$n" "${made[$n]}"; done
    ;;
  add)
    name="${1:?usage: add NAME}"; valid_name "$name"
    k="$(new_key)"
    read_keys | python3 -c '
import json, sys
d = json.load(sys.stdin)
if sys.argv[1] in d:
    sys.exit(f"{sys.argv[1]} already has a key; remove it first")
d[sys.argv[1]] = sys.argv[2]
print(json.dumps(d))' "$name" "$k" | write_keys
    echo "Added a key for $name."
    print_key "$name" "$k"
    ;;
  remove)
    name="${1:?usage: remove NAME}"
    read_keys | python3 -c '
import json, sys
d = json.load(sys.stdin)
if sys.argv[1] not in d:
    sys.exit(f"No key named {sys.argv[1]}")
del d[sys.argv[1]]
print(json.dumps(d))' "$name" | write_keys
    echo "Revoked the key for $name."
    ;;
  list)
    read_keys | python3 -c 'import json,sys; print("\n".join(sorted(json.load(sys.stdin))) or "(no keys)")'
    ;;
  open)
    service_exists || { echo "Deploy first (merge to main), then run this."; exit 1; }
    # Requests skip Cloud Run's Google-account check; the server rejects anything
    # without a valid API key (401) before it reaches a tool.
    gcloud run services update "$SERVICE" --region "$REGION" "${P[@]}" --no-invoker-iam-check --quiet >/dev/null
    url="$(gcloud run services describe "$SERVICE" --region "$REGION" "${P[@]}" --format='value(status.url)')"
    echo "Open. Health check: $(curl -s "$url/healthz")"
    echo "Without a key: HTTP $(curl -s -o /dev/null -w '%{http_code}' -X POST "$url/mcp") (expected 401)"
    ;;
  url)
    url="$(gcloud run services describe "$SERVICE" --region "$REGION" "${P[@]}" --format='value(status.url)')"
    echo "$url/mcp"
    ;;
  *)
    sed -n '2,15p' "$0"; exit 1
    ;;
esac
