#!/bin/bash
# One-time setup for .github/workflows/deploy.yml: lets GitHub Actions on this
# repository's main branch deploy the Firebase project, with no stored key.
#
# Creates a deploy service account, a Workload Identity pool and provider that
# accept only GitHub tokens for this repository's main branch, and lets that
# provider act as the service account. Safe to re-run.
#
# Run it on your own machine, signed in with `gcloud auth login` as a project
# owner. Needs gcloud, curl and python3; with the GitHub CLI (gh) signed in,
# it also sets the two repository variables the workflow reads.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-open-hex}"
REPO="${REPO:-bovard/open-hex}"
SA_NAME="github-deploy"
POOL="github"
PROVIDER="github-actions"

SA="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
# The repository's numeric ID, so a deleted and re-created repository of the
# same name is not trusted
REPO_ID="$(curl -fsS "https://api.github.com/repos/${REPO}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"

echo "Project ${PROJECT_ID} (${PROJECT_NUMBER}), repository ${REPO} (${REPO_ID})"

gcloud services enable --project "$PROJECT_ID" \
  iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com

if ! gcloud iam service-accounts describe "$SA" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT_ID" \
    --display-name "GitHub Actions deploy (${REPO})"
fi

# What `firebase deploy` needs for this project: Python (2nd gen) functions
# with scheduled triggers, hosting, Firestore rules and indexes, storage rules
for role in \
  roles/cloudfunctions.admin \
  roles/run.admin \
  roles/cloudscheduler.admin \
  roles/artifactregistry.writer \
  roles/cloudbuild.builds.editor \
  roles/firebasehosting.admin \
  roles/firebaserules.admin \
  roles/firebase.viewer \
  roles/datastore.indexAdmin \
  roles/firebaseextensions.viewer \
  roles/serviceusage.serviceUsageConsumer
do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --condition=None --quiet \
    --member "serviceAccount:${SA}" --role "$role" >/dev/null
done

# The functions run as the default compute service account; deploying them
# means acting as it
gcloud iam service-accounts add-iam-policy-binding \
  "${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" --project "$PROJECT_ID" --quiet \
  --member "serviceAccount:${SA}" --role roles/iam.serviceAccountUser >/dev/null

if ! gcloud iam workload-identity-pools describe "$POOL" --project "$PROJECT_ID" --location global >/dev/null 2>&1; then
  gcloud iam workload-identity-pools create "$POOL" --project "$PROJECT_ID" --location global \
    --display-name "GitHub Actions"
fi

CONDITION="assertion.repository_id == '${REPO_ID}' && assertion.ref == 'refs/heads/main'"
MAPPING="google.subject=assertion.sub,attribute.repository_id=assertion.repository_id,attribute.ref=assertion.ref"
if gcloud iam workload-identity-pools providers describe "$PROVIDER" --project "$PROJECT_ID" \
    --location global --workload-identity-pool "$POOL" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools providers update-oidc "$PROVIDER" --project "$PROJECT_ID" \
    --location global --workload-identity-pool "$POOL" \
    --attribute-mapping "$MAPPING" --attribute-condition "$CONDITION"
else
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" --project "$PROJECT_ID" \
    --location global --workload-identity-pool "$POOL" \
    --issuer-uri "https://token.actions.githubusercontent.com" \
    --attribute-mapping "$MAPPING" --attribute-condition "$CONDITION"
fi

POOL_ID="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}"
gcloud iam service-accounts add-iam-policy-binding "$SA" --project "$PROJECT_ID" --quiet \
  --member "principalSet://iam.googleapis.com/${POOL_ID}/attribute.repository_id/${REPO_ID}" \
  --role roles/iam.workloadIdentityUser >/dev/null

PROVIDER_ID="${POOL_ID}/providers/${PROVIDER}"
echo
echo "Done. Repository variables for the workflow (not secrets):"
echo "  GCP_WORKLOAD_IDENTITY_PROVIDER=${PROVIDER_ID}"
echo "  GCP_SERVICE_ACCOUNT=${SA}"

if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  gh variable set GCP_WORKLOAD_IDENTITY_PROVIDER --repo "$REPO" --body "$PROVIDER_ID"
  gh variable set GCP_SERVICE_ACCOUNT --repo "$REPO" --body "$SA"
  echo "Set both on ${REPO}."
else
  echo "Add them under ${REPO} > Settings > Secrets and variables > Actions > Variables."
fi
