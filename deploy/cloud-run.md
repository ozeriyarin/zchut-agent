# Deploying the Telegram webhook to Cloud Run

The owner runs these commands; the agent never deploys. `scripts/deploy.ps1` wraps the same
commands (`.\scripts\deploy.ps1 -Step check|setup|tighten|secrets|build|deploy|webhook|all`). Its
parameters `-Project`, `-Region`, `-QdrantUrl` and `-AllowedChatIds` default to the environment
variables `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_REGION`, `QDRANT_URL` and
`TELEGRAM_ALLOWED_CHAT_IDS`, then to the same lines of the repo-root `.env` (it reads only these
four names from it), then to `zchut-agent` / `europe-west1` / empty. `-BillingAccount` defaults to
empty. The `secrets` step offers each secret found in `.env` (`[Enter = use .env value]`).

## Prerequisites

1. Install the Google Cloud CLI: https://cloud.google.com/sdk/docs/install (open a new terminal).
2. `gcloud auth login`
3. `gcloud auth application-default login` (Firestore from the laptop, for `zchut cost`).
4. `gcloud config set project <project id>`
5. `.\scripts\deploy.ps1 -Step check` (gcloud on PATH, an active account, the project set).

```powershell
$Project = "zchut-agent"                    # or the id of a project created in the console
$Region = "europe-west1"
$BillingAccount = "000000-000000-000000"   # gcloud billing accounts list
$Service = "zchut-bot"
$Repo = "zchut"
$RunAs = "zchut-bot@$Project.iam.gserviceaccount.com"
$BuildAs = "zchut-build@$Project.iam.gserviceaccount.com"
$BuildBucket = "gs://$($Project)_cloudbuild"
$QdrantUrl = "https://YOUR-CLUSTER.europe-west3-0.gcp.cloud.qdrant.io:6333"
$AllowedChatIds = ""                        # "123,456"; empty lets everyone in
$Image = "$Region-docker.pkg.dev/$Project/$Repo/$Service`:latest"
```

## 1. Project, billing and budget alert

Cloud Run needs billing enabled even inside the free tier. `--max-instances 1` caps the worst
case, and the budget alert mails at 1 and 2 USD.

The project, its billing link and the budgets may already exist (created in the console). The
script's `setup` step then skips `projects create`, `billing projects link` and
`billing budgets create`: it skips them when `gcloud projects describe` finds the project or when
`-BillingAccount` is empty, and always runs `config set project` and `services enable`.

```powershell
gcloud projects create $Project --name $Project
gcloud billing projects link $Project --billing-account $BillingAccount
gcloud config set project $Project
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com `
  secretmanager.googleapis.com firestore.googleapis.com billingbudgets.googleapis.com
gcloud billing budgets create --billing-account $BillingAccount --display-name "$Service budget" `
  --budget-amount 2USD --threshold-rule percent=0.5 --threshold-rule percent=1.0
```

## 2. Firestore with a 30-minute session TTL

Sessions live in `sessions/<chat_id>` with an `expires_at` timestamp 30 minutes after the last
message. The TTL policy deletes expired documents (Firestore does this within about a day of
`expires_at`; the bot also ignores an expired session on read, so it never resumes one).
Run records go to the `runs` collection (hashes only, no TTL).

```powershell
gcloud firestore databases create --location $Region --type firestore-native
gcloud firestore fields ttls update expires_at --collection-group sessions --enable-ttl
gcloud firestore fields ttls list --collection-group sessions
```

## 3. Artifact Registry, the runtime account and the build account

```powershell
gcloud artifacts repositories create $Repo --repository-format docker --location $Region
gcloud iam service-accounts create zchut-bot --display-name "zchut bot runtime"
gcloud projects add-iam-policy-binding $Project --member "serviceAccount:$RunAs" `
  --role roles/secretmanager.secretAccessor --condition None
gcloud projects add-iam-policy-binding $Project --member "serviceAccount:$RunAs" `
  --role roles/datastore.user --condition None

# the build account; the script skips the create and the bucket create when they already exist
gcloud iam service-accounts create zchut-build --display-name "zchut Cloud Build"
gcloud storage buckets create $BuildBucket --location $Region --uniform-bucket-level-access
gcloud projects add-iam-policy-binding $Project --member "serviceAccount:$BuildAs" `
  --role roles/logging.logWriter --condition None
gcloud storage buckets add-iam-policy-binding $BuildBucket --member "serviceAccount:$BuildAs" `
  --role roles/storage.objectViewer
gcloud storage buckets add-iam-policy-binding $BuildBucket --member "serviceAccount:$BuildAs" `
  --role roles/storage.objectCreator
gcloud artifacts repositories add-iam-policy-binding $Repo --location $Region `
  --member "serviceAccount:$BuildAs" --role roles/artifactregistry.writer
```

### Why a dedicated build account

New projects run Cloud Build as the default compute service account, and the earlier setup gave
that account `roles/cloudbuild.builds.builder` on the whole project: every VM or service that
falls back to the default account could then read every bucket, push to every registry and start
builds. `zchut-build` holds only what one build needs: read the uploaded source and write the build
log in `$BuildBucket`, write log entries, and push to the one `zchut` repository. A leaked build
step can then touch nothing else.

`.\scripts\deploy.ps1 -Step tighten` moves a project that was set up before this change: it
revokes `roles/cloudbuild.builds.builder` from `<project number>-compute@developer.gserviceaccount.com`
when that account holds it (and says so when it does not), then runs the build-account commands
above. It is safe to re-run; `-Step all` runs it right after `setup`.

```powershell
$Number = gcloud projects describe $Project --format "value(projectNumber)"
gcloud projects remove-iam-policy-binding $Project `
  --member "serviceAccount:$Number-compute@developer.gserviceaccount.com" `
  --role roles/cloudbuild.builds.builder --all
```

## 4. Secrets in Secret Manager

Nothing secret goes into the image. Each value is written to a temporary file without a trailing
newline, then the file is deleted. `TELEGRAM_WEBHOOK_SECRET` may only use `A-Z a-z 0-9 _ -`.

```powershell
foreach ($name in "TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "QDRANT_API_KEY", "VOYAGE_API_KEY", "ZCHUT_ANTHROPIC_API_KEY") {
  $secure = Read-Host $name -AsSecureString
  $tmp = New-TemporaryFile
  [IO.File]::WriteAllText($tmp, [Net.NetworkCredential]::new("", $secure).Password)
  gcloud secrets create $name --replication-policy automatic
  gcloud secrets versions add $name --data-file $tmp
  Remove-Item $tmp
}
```

## 5. Build with Cloud Build

```powershell
gcloud builds submit --region $Region --tag $Image `
  --service-account "projects/$Project/serviceAccounts/$BuildAs" --gcs-log-dir "$BuildBucket/logs" .
```

The build runs as `zchut-build` (section 3), not the default compute account. Cloud Build refuses
a build with its own service account unless the logs have a home: `--tag` builds have no logging
option, so `--gcs-log-dir` puts them in the same bucket as the source, and they still reach Cloud
Logging through `roles/logging.logWriter`.

## 6. Deploy

The webhook answers 200 at once and keeps working on the update in a background task, so the CPU
must stay allocated after the response: `--no-cpu-throttling` (instance-based billing; the
instance still scales to zero after it goes idle). If that ever costs too much, set
`bot.process_inline` to `true` in `config/zchut.json` and drop the flag: the reply is then sent
before the 200, which Telegram tolerates for answers of a few seconds.

The `^;^` prefix makes `;` the separator so the comma-separated chat ids survive.

```powershell
gcloud run deploy $Service --region $Region --image $Image --service-account $RunAs `
  --min-instances 0 --max-instances 1 --concurrency 4 --memory 1Gi --cpu 1 --no-cpu-throttling `
  --allow-unauthenticated `
  --set-env-vars "^;^GOOGLE_CLOUD_PROJECT=$Project;QDRANT_URL=$QdrantUrl;TELEGRAM_ALLOWED_CHAT_IDS=$AllowedChatIds" `
  --set-secrets "TELEGRAM_BOT_TOKEN=TELEGRAM_BOT_TOKEN:latest,TELEGRAM_WEBHOOK_SECRET=TELEGRAM_WEBHOOK_SECRET:latest,QDRANT_API_KEY=QDRANT_API_KEY:latest,VOYAGE_API_KEY=VOYAGE_API_KEY:latest,ZCHUT_ANTHROPIC_API_KEY=ZCHUT_ANTHROPIC_API_KEY:latest"
```

`--allow-unauthenticated` is required because Telegram cannot send Google credentials; the
`X-Telegram-Bot-Api-Secret-Token` header check (403 otherwise) and the unguessable path guard it.
The deployed webhook always reads Qdrant, whatever `store.backend` says.

## 7. Telegram webhook

The path is `/telegram/<first 16 hex chars of sha256(TELEGRAM_WEBHOOK_SECRET)>`, so the secret
itself never appears in request logs; Telegram sends the secret in the header.

```powershell
$Url = gcloud run services describe $Service --region $Region --format "value(status.url)"
$Token = gcloud secrets versions access latest --secret TELEGRAM_BOT_TOKEN
$Secret = gcloud secrets versions access latest --secret TELEGRAM_WEBHOOK_SECRET
$Sha = [Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($Secret))
$HookId = ([BitConverter]::ToString($Sha) -replace "-", "").ToLower().Substring(0, 16)
Invoke-RestMethod -Method Post -Uri "https://api.telegram.org/bot$Token/setWebhook" -Body @{
  url = "$Url/telegram/$HookId"; secret_token = $Secret
  allowed_updates = '["message"]'; drop_pending_updates = "true"
}
Invoke-RestMethod -Uri "https://api.telegram.org/bot$Token/getWebhookInfo"
```

## 8. Corpus and costs from the owner's machine

```powershell
# QDRANT_URL and QDRANT_API_KEY in .env; copies the local Chroma folder and checks the counts
zchut store migrate --from chroma --to qdrant
# then set "store": {"backend": "qdrant"} in config/zchut.json so ingest, crawl and ask use Qdrant

gcloud auth application-default login
zchut cost --source firestore --days 7   # needs GOOGLE_CLOUD_PROJECT in .env
```

## Redeploy, pause, remove

```powershell
.\scripts\deploy.ps1 -Step build; .\scripts\deploy.ps1 -Step deploy
Invoke-RestMethod -Method Post -Uri "https://api.telegram.org/bot$Token/deleteWebhook"   # pause
gcloud run services delete $Service --region $Region                                      # remove
```
