param(
    [ValidateSet("check", "setup", "tighten", "secrets", "build", "deploy", "webhook", "all")]
    [string]$Step = "all",
    [string]$Project = "",
    [string]$Region = "",
    [string]$QdrantUrl = "",
    [string]$AllowedChatIds = "",
    [string]$BillingAccount = ""
)

$ErrorActionPreference = "Stop"
# gcloud prints progress and NOT_FOUND to stderr; only exit codes decide failure here.
$PSNativeCommandUseErrorActionPreference = $false

$DotenvPath = Join-Path (Split-Path -Parent $PSScriptRoot) ".env"
$ConfigNames = @("GOOGLE_CLOUD_PROJECT", "QDRANT_URL", "TELEGRAM_ALLOWED_CHAT_IDS", "GOOGLE_CLOUD_REGION")

# Returns only the requested names from the repo-root .env; never prints values.
function Read-Dotenv([string[]]$Names) {
    $found = @{}
    if (-not (Test-Path -LiteralPath $DotenvPath)) { return $found }
    foreach ($line in [IO.File]::ReadAllLines($DotenvPath)) {
        $key, $value = $line.Trim() -split "=", 2
        $key = $key.Trim()
        if ($null -eq $value -or $key.StartsWith("#") -or $Names -notcontains $key) { continue }
        $value = $value.Trim()
        if ($value.Length -ge 2 -and $value[0] -eq $value[-1] -and "`"'".Contains($value[0])) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        if ($value) { $found[$key] = $value }
    }
    return $found
}

function Get-Setting([string]$Name, [string]$Fallback) {
    $fromEnv = [Environment]::GetEnvironmentVariable($Name)
    if ($fromEnv) { return $fromEnv }
    if ($DotenvConfig.ContainsKey($Name)) { return $DotenvConfig[$Name] }
    return $Fallback
}

$DotenvConfig = Read-Dotenv $ConfigNames
if (-not $PSBoundParameters.ContainsKey("Project")) { $Project = Get-Setting "GOOGLE_CLOUD_PROJECT" "zchut-agent" }
if (-not $PSBoundParameters.ContainsKey("Region")) { $Region = Get-Setting "GOOGLE_CLOUD_REGION" "europe-west1" }
if (-not $PSBoundParameters.ContainsKey("QdrantUrl")) { $QdrantUrl = Get-Setting "QDRANT_URL" "" }
if (-not $PSBoundParameters.ContainsKey("AllowedChatIds")) { $AllowedChatIds = Get-Setting "TELEGRAM_ALLOWED_CHAT_IDS" "" }

$Service = "zchut-bot"
$Repo = "zchut"
$ServiceAccount = "zchut-bot"
$BuildAccount = "zchut-build"
$BudgetUsd = 2

$Image = "$Region-docker.pkg.dev/$Project/$Repo/$Service`:latest"
$RunAs = "$ServiceAccount@$Project.iam.gserviceaccount.com"
$BuildAs = "$BuildAccount@$Project.iam.gserviceaccount.com"
$BuildBucket = "gs://$($Project)_cloudbuild"
$SecretNames = @(
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "QDRANT_API_KEY",
    "VOYAGE_API_KEY", "ZCHUT_ANTHROPIC_API_KEY"
)

function Invoke-Gcloud {
    & gcloud @args
    if ($LASTEXITCODE -ne 0) { throw "gcloud $($args -join ' ') failed" }
}

# cmd swallows gcloud's stderr so PowerShell 5.1/7 never turns NOT_FOUND into an error record
function Test-Gcloud([string]$Command) {
    cmd /c "gcloud $Command >nul 2>&1"
    return $LASTEXITCODE -eq 0
}

function Set-Secret([string]$Name, [string]$Value) {
    $tmp = New-TemporaryFile
    try {
        [IO.File]::WriteAllText($tmp, $Value)
        if (-not (Test-Gcloud "secrets describe $Name --project $Project")) {
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
    & gcloud projects describe $Project *> $null
    $exists = $LASTEXITCODE -eq 0
    if ($exists -or -not $BillingAccount) {
        $why = if ($exists) { "project $Project already exists" } else { "no -BillingAccount" }
        Write-Host "Skipping project create, billing link and budget create: $why."
    } else {
        Invoke-Gcloud projects create $Project --name $Project
        Invoke-Gcloud billing projects link $Project --billing-account $BillingAccount
    }
    Invoke-Gcloud config set project $Project
    Invoke-Gcloud services enable run.googleapis.com cloudbuild.googleapis.com `
        artifactregistry.googleapis.com secretmanager.googleapis.com firestore.googleapis.com `
        billingbudgets.googleapis.com
    if (-not $exists -and $BillingAccount) {
        Invoke-Gcloud billing budgets create --billing-account $BillingAccount `
            --display-name "$Service budget" --budget-amount "$($BudgetUsd)USD" `
            --threshold-rule percent=0.5 --threshold-rule percent=1.0
    }
    Invoke-Gcloud firestore databases create --location $Region --type firestore-native
    Invoke-Gcloud firestore fields ttls update expires_at --collection-group sessions --enable-ttl
    Invoke-Gcloud artifacts repositories create $Repo --repository-format docker --location $Region
    Invoke-Gcloud iam service-accounts create $ServiceAccount --display-name "zchut bot runtime"
    foreach ($role in @("roles/secretmanager.secretAccessor", "roles/datastore.user")) {
        Invoke-Gcloud projects add-iam-policy-binding $Project `
            --member "serviceAccount:$RunAs" --role $role --condition None
    }
    Grant-BuildAccount
}

function Grant-BuildAccount {
    if (-not (Test-Gcloud "iam service-accounts describe $BuildAs --project $Project")) {
        Invoke-Gcloud iam service-accounts create $BuildAccount --project $Project `
            --display-name "zchut Cloud Build"
    }
    if (-not (Test-Gcloud "storage buckets describe $BuildBucket --project $Project")) {
        Invoke-Gcloud storage buckets create $BuildBucket --project $Project --location $Region `
            --uniform-bucket-level-access
    }
    $member = "serviceAccount:$BuildAs"
    Invoke-Gcloud projects add-iam-policy-binding $Project --member $member `
        --role roles/logging.logWriter --condition None
    foreach ($role in @("roles/storage.objectViewer", "roles/storage.objectCreator")) {
        Invoke-Gcloud storage buckets add-iam-policy-binding $BuildBucket `
            --member $member --role $role
    }
    Invoke-Gcloud artifacts repositories add-iam-policy-binding $Repo --project $Project `
        --location $Region --member $member --role roles/artifactregistry.writer
}

function Step-Tighten {
    $number = (& gcloud projects describe $Project --format "value(projectNumber)") -join ""
    if ($LASTEXITCODE -ne 0 -or -not $number) { throw "cannot read the project number of $Project" }
    $compute = "serviceAccount:$number-compute@developer.gserviceaccount.com"
    $role = "roles/cloudbuild.builds.builder"
    $held = & gcloud projects get-iam-policy $Project --flatten "bindings[].members" `
        --filter "bindings.role=$role AND bindings.members=$compute" --format "value(bindings.role)"
    if ($LASTEXITCODE -ne 0) { throw "cannot read the IAM policy of $Project" }
    if ($held) {
        Invoke-Gcloud projects remove-iam-policy-binding $Project `
            --member $compute --role $role --all
    } else {
        Write-Host "Skipping revoke: $compute does not hold $role."
    }
    Grant-BuildAccount
}

function Step-Secrets {
    $alphabet = [char[]]"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
    $generated = -join (1..48 | ForEach-Object { $alphabet | Get-Random })
    foreach ($name in $SecretNames) {
        if ($name -eq "TELEGRAM_WEBHOOK_SECRET") {
            Set-Secret $name $generated
            continue
        }
        $fromDotenv = (Read-Dotenv @($name))[$name]
        $prompt = if ($fromDotenv) { "$name [Enter = use .env value]" } else { $name }
        $secure = Read-Host $prompt -AsSecureString
        $value = [Net.NetworkCredential]::new("", $secure).Password
        if (-not $value) { $value = $fromDotenv }
        if (-not $value) { throw "no value for $name" }
        Set-Secret $name $value
    }
}

function Step-Check {
    if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
        Write-Host "gcloud is not on PATH. Install it: https://cloud.google.com/sdk/docs/install"
        exit 1
    }
    $account = & gcloud auth list --filter "status:ACTIVE" --format "value(account)"
    if ($LASTEXITCODE -ne 0 -or -not $account) {
        Write-Host "No active gcloud account. Run: gcloud auth login"
        exit 1
    }
    $configured = (& gcloud config get-value project 2> $null) -join ""
    if ($configured -ne $Project) {
        Write-Host "gcloud project is '$configured', expected $Project. Run: gcloud config set project $Project"
        exit 1
    }
    $uri = $null
    $qdrantHost = if ([Uri]::TryCreate($QdrantUrl, "Absolute", [ref]$uri)) { $uri.Host } else { "(not set)" }
    $chatCount = @($AllowedChatIds -split "," | Where-Object { $_.Trim() }).Count
    Write-Host "Project: $Project"
    Write-Host "Region: $Region"
    Write-Host "QdrantUrl host: $qdrantHost"
    Write-Host "AllowedChatIds: $(if ($chatCount) { "$chatCount ids" } else { 'not set (everyone allowed)' })"
}

function Step-Build {
    Invoke-Gcloud builds submit --project $Project --region $Region --tag $Image `
        --service-account "projects/$Project/serviceAccounts/$BuildAs" `
        --gcs-log-dir "$BuildBucket/logs" .
}

function Step-Deploy {
    if (-not $QdrantUrl) { throw "QdrantUrl is empty: pass -QdrantUrl or set QDRANT_URL in .env" }
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
    "check" { Step-Check }
    "setup" { Step-Setup }
    "tighten" { Step-Tighten }
    "secrets" { Step-Secrets }
    "build" { Step-Build }
    "deploy" { Step-Deploy }
    "webhook" { Step-Webhook }
    "all" { Step-Check; Step-Setup; Step-Tighten; Step-Secrets; Step-Build; Step-Deploy; Step-Webhook }
}
