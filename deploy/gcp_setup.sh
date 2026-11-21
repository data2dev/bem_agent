#!/usr/bin/env bash
# One-time GCP setup for bem_agent. Run it yourself in Cloud Shell after reading it.
#
#   git clone https://github.com/data2dev/bem_agent && cd bem_agent
#   bash deploy/gcp_setup.sh
#
# What it creates, all inside PROJECT_ID only (see "Safety guardrails" in ROADMAP.md):
#   - APIs: Cloud Run, Artifact Registry, Secret Manager, Cloud Storage, IAM, STS
#   - Artifact Registry Docker repo   (container images)
#   - Cloud Storage bucket            (simulation outputs and results.db)
#   - Secret GROQ_API_KEY             (you paste the key; it is never echoed or logged)
#   - Service account bem-runtime     (what Cloud Run runs as: its bucket + the secret only)
#   - Service account bem-deployer    (what GitHub Actions deploys as: push images, deploy)
#   - Workload Identity Federation    (GitHub login without key files; this repo's main
#                                      branch only)
#
# Every gcloud command names --project explicitly. Safe to re-run: existing resources
# are left as they are.

set -euo pipefail

PROJECT_ID="projectbem"
REGION="us-east1"
GITHUB_REPO="data2dev/bem_agent"
AR_REPO="bem-agent"
BUCKET="${PROJECT_ID}-bem-runs"
RUNTIME_SA_NAME="bem-runtime"
DEPLOY_SA_NAME="bem-deployer"
WIF_POOL="github"
WIF_PROVIDER="bem-agent-repo"
SECRET="GROQ_API_KEY"

P=(--project="$PROJECT_ID")
RUNTIME_SA="${RUNTIME_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
DEPLOY_SA="${DEPLOY_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

step() { printf '\n==> %s\n' "$*"; }
exists() { "$@" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------
step "Checking project ${PROJECT_ID}"
gcloud projects describe "$PROJECT_ID" --format='value(name,projectNumber)'
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"

billing="$(gcloud billing projects describe "$PROJECT_ID" --format='value(billingEnabled)' 2>/dev/null || true)"
if [[ "$billing" != "True" ]]; then
  echo "Billing is not enabled for ${PROJECT_ID}. Cloud Run needs a billing account linked."
  echo "Link one at https://console.cloud.google.com/billing/linkedaccount?project=${PROJECT_ID}"
  echo "and set a budget alert, then re-run this script."
  exit 1
fi

echo
echo "This will create resources in project ${PROJECT_ID} (number ${PROJECT_NUMBER}), region ${REGION}."
read -rp "Type the project ID to continue: " confirm
[[ "$confirm" == "$PROJECT_ID" ]] || { echo "Aborted."; exit 1; }

# ---------------------------------------------------------------------------
step "Enabling APIs"
gcloud services enable "${P[@]}" \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  storage.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  sts.googleapis.com

# ---------------------------------------------------------------------------
step "Artifact Registry repo ${AR_REPO}"
if ! exists gcloud artifacts repositories describe "$AR_REPO" --location="$REGION" "${P[@]}"; then
  gcloud artifacts repositories create "$AR_REPO" "${P[@]}" \
    --repository-format=docker --location="$REGION" \
    --description="bem_agent container images"
fi

step "Cloud Storage bucket gs://${BUCKET}"
if ! exists gcloud storage buckets describe "gs://${BUCKET}" "${P[@]}"; then
  gcloud storage buckets create "gs://${BUCKET}" "${P[@]}" \
    --location="$REGION" --uniform-bucket-level-access --public-access-prevention
fi

# ---------------------------------------------------------------------------
step "Secret ${SECRET}"
if exists gcloud secrets describe "$SECRET" "${P[@]}"; then
  echo "Secret already exists. To replace the key later:"
  echo "  gcloud secrets versions add ${SECRET} --data-file=- --project=${PROJECT_ID}"
else
  read -rsp "Paste your Groq API key (input hidden), then press Enter: " GROQ_KEY
  echo
  [[ -n "$GROQ_KEY" ]] || { echo "Empty key; aborted."; exit 1; }
  # Drop terminal escape codes (arrow keys, bracketed paste) and whitespace.
  GROQ_KEY="$(printf '%s' "$GROQ_KEY" | sed $'s/\x1b\\[[0-9;]*[A-Za-z~]//g' | tr -d '[:space:]"')"
  [[ "$GROQ_KEY" =~ ^gsk_[A-Za-z0-9]+$ ]] || echo "Warning: key doesn't look like a Groq key (gsk_...)."
  printf '%s' "$GROQ_KEY" | gcloud secrets create "$SECRET" "${P[@]}" \
    --replication-policy=automatic --data-file=-
  unset GROQ_KEY
fi

# ---------------------------------------------------------------------------
step "Service accounts"
if ! exists gcloud iam service-accounts describe "$RUNTIME_SA" "${P[@]}"; then
  gcloud iam service-accounts create "$RUNTIME_SA_NAME" "${P[@]}" \
    --display-name="bem_agent Cloud Run runtime"
fi
if ! exists gcloud iam service-accounts describe "$DEPLOY_SA" "${P[@]}"; then
  gcloud iam service-accounts create "$DEPLOY_SA_NAME" "${P[@]}" \
    --display-name="bem_agent GitHub Actions deployer"
fi

step "Runtime permissions: its bucket and the Groq secret only"
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" "${P[@]}" \
  --member="serviceAccount:${RUNTIME_SA}" --role="roles/storage.objectAdmin" >/dev/null
gcloud secrets add-iam-policy-binding "$SECRET" "${P[@]}" \
  --member="serviceAccount:${RUNTIME_SA}" --role="roles/secretmanager.secretAccessor" >/dev/null

step "Deployer permissions: push images, deploy Cloud Run, act as the runtime account"
gcloud artifacts repositories add-iam-policy-binding "$AR_REPO" "${P[@]}" \
  --location="$REGION" \
  --member="serviceAccount:${DEPLOY_SA}" --role="roles/artifactregistry.writer" >/dev/null
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${DEPLOY_SA}" --role="roles/run.developer" \
  --condition=None >/dev/null
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" "${P[@]}" \
  --member="serviceAccount:${DEPLOY_SA}" --role="roles/iam.serviceAccountUser" >/dev/null

# ---------------------------------------------------------------------------
step "Workload Identity Federation for ${GITHUB_REPO} (main branch only)"
if ! exists gcloud iam workload-identity-pools describe "$WIF_POOL" --location=global "${P[@]}"; then
  gcloud iam workload-identity-pools create "$WIF_POOL" "${P[@]}" \
    --location=global --display-name="GitHub Actions"
fi
if ! exists gcloud iam workload-identity-pools providers describe "$WIF_PROVIDER" \
    --workload-identity-pool="$WIF_POOL" --location=global "${P[@]}"; then
  gcloud iam workload-identity-pools providers create-oidc "$WIF_PROVIDER" "${P[@]}" \
    --location=global --workload-identity-pool="$WIF_POOL" \
    --display-name="bem_agent repo" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
    --attribute-condition="assertion.repository=='${GITHUB_REPO}' && assertion.ref=='refs/heads/main'"
fi
gcloud iam service-accounts add-iam-policy-binding "$DEPLOY_SA" "${P[@]}" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${WIF_POOL}/attribute.repository/${GITHUB_REPO}" \
  >/dev/null

# ---------------------------------------------------------------------------
step "Done. Send these values back (they are identifiers, not secrets):"
cat <<EOF
GCP_PROJECT_ID=${PROJECT_ID}
GCP_REGION=${REGION}
GCP_WIF_PROVIDER=projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${WIF_POOL}/providers/${WIF_PROVIDER}
GCP_DEPLOY_SA=${DEPLOY_SA}
GCP_RUNTIME_SA=${RUNTIME_SA}
GCP_AR_REPO=${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}
GCP_BUCKET=${BUCKET}
EOF
