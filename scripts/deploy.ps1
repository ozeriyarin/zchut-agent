param(
    [ValidateSet("setup", "secrets", "build", "deploy", "webhook", "all")]
    [string]$Step = "all"
)

$ErrorActionPreference = "Stop"

$Project = "zchut-agent"
$Region = "europe-west1"
$BillingAccount = "000000-000000-000000"
$Service = "zchut-bot"
$Repo = "zchut"
$ServiceAccount = "zchut-bot"
$QdrantUrl = "https://YOUR-CLUSTER.europe-west3-0.gcp.cloud.qdrant.io:6333"
$AllowedChatIds = ""
$BudgetUsd = 2

$Image = "$Region-docker.pkg.dev/$Project/$Repo/$Service`:latest"
$RunAs = "$ServiceAccount@$Project.iam.gserviceaccount.com"
$SecretNames = @(
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "QDRANT_API_KEY",
    "VOYAGE_API_KEY", "ZCHUT_ANTHROPIC_API_KEY"
)

function Invoke-Gcloud {
    & gcloud @args
    if ($LASTEXITCODE -ne 0) { throw "gcloud $($args -join ' ') failed" }
}

function Set-Secret([string]$Name, [string]$Value) {
    $tmp = New-TemporaryFile
    try {
        [IO.File]::WriteAllText($tmp, $Value)
        & gcloud secrets describe $Name --project $Project *> $null
        if ($LASTEXITCODE -ne 0) {
            Invoke-Gcloud secrets create $Name --project $Project --replication-policy automatic
        }
        Invoke-Gcloud secrets versions add $Name --project $Project --data-file $tmp
    } finally {
        Remove-Item $tmp -Force
    }
}

function Get-Secret([string]$Name) {
    $value = & gcloud secrets versions access latest --secret $Name --project $Project
    if ($LASTEXITCODE -ne 0) { throw "cannot read secret $Name" }
    return ($value -join "")
}

function Get-HookId([string]$Secret) {
    $sha = [Security.Cryptography.SHA256]::Create()
    $bytes = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Secret))
    return ([BitConverter]::ToString($bytes) -replace "-", "").ToLower().Substring(0, 16)
}

function Step-Setup {
    Invoke-Gcloud projects create $Project --name $Project
    Invoke-Gcloud billing projects link $Project --billing-account $BillingAccount
    Invoke-Gcloud config set project $Project
    Invoke-Gcloud services enable run.googleapis.com cloudbuild.googleapis.com `
        artifactregistry.googleapis.com secretmanager.googleapis.com firestore.googleapis.com `
        billingbudgets.googleapis.com
    Invoke-Gcloud billing budgets create --billing-account $BillingAccount `
        --display-name "$Service budget" --budget-amount "$($BudgetUsd)USD" `
        --threshold-rule percent=0.5 --threshold-rule percent=1.0
    Invoke-Gcloud firestore databases create --location $Region --type firestore-native
    Invoke-Gcloud firestore fields ttls update expires_at --collection-group sessions --enable-ttl
    Invoke-Gcloud artifacts repositories create $Repo --repository-format docker --location $Region
    Invoke-Gcloud iam service-accounts create $ServiceAccount --display-name "zchut bot runtime"
    foreach ($role in @("roles/secretmanager.secretAccessor", "roles/datastore.user")) {
        Invoke-Gcloud projects add-iam-policy-binding $Project `
            --member "serviceAccount:$RunAs" --role $role --condition None
    }
}

function Step-Secrets {
    $alphabet = [char[]]"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
    $generated = -join (1..48 | ForEach-Object { $alphabet | Get-Random })
    foreach ($name in $SecretNames) {
        if ($name -eq "TELEGRAM_WEBHOOK_SECRET") {
            Set-Secret $name $generated
            continue
        }
        $secure = Read-Host "$name" -AsSecureString
        Set-Secret $name ([Net.NetworkCredential]::new("", $secure).Password)
    }
}

function Step-Build {
    Invoke-Gcloud builds submit --project $Project --region $Region --tag $Image .
}

function Step-Deploy {
    $envVars = "^;^GOOGLE_CLOUD_PROJECT=$Project;QDRANT_URL=$QdrantUrl;TELEGRAM_ALLOWED_CHAT_IDS=$AllowedChatIds"
    $secrets = ($SecretNames | ForEach-Object { "$_=$($_):latest" }) -join ","
    Invoke-Gcloud run deploy $Service --project $Project --region $Region --image $Image `
        --service-account $RunAs --min-instances 0 --max-instances 1 --concurrency 4 `
        --memory 1Gi --cpu 1 --no-cpu-throttling --allow-unauthenticated `
        --set-env-vars $envVars --set-secrets $secrets
}

function Step-Webhook {
    $url = (& gcloud run services describe $Service --project $Project --region $Region `
            --format "value(status.url)")
    $token = Get-Secret "TELEGRAM_BOT_TOKEN"
    $secret = Get-Secret "TELEGRAM_WEBHOOK_SECRET"
    $body = @{
        url                  = "$url/telegram/$(Get-HookId $secret)"
        secret_token         = $secret
        allowed_updates      = '["message"]'
        drop_pending_updates = "true"
    }
    $set = Invoke-RestMethod -Method Post -Uri "https://api.telegram.org/bot$token/setWebhook" -Body $body
    Write-Host "setWebhook ok: $($set.ok)"
    $info = Invoke-RestMethod -Uri "https://api.telegram.org/bot$token/getWebhookInfo"
    Write-Host "pending updates: $($info.result.pending_update_count)"
}

switch ($Step) {
    "setup" { Step-Setup }
    "secrets" { Step-Secrets }
    "build" { Step-Build }
    "deploy" { Step-Deploy }
    "webhook" { Step-Webhook }
    "all" { Step-Setup; Step-Secrets; Step-Build; Step-Deploy; Step-Webhook }
}
