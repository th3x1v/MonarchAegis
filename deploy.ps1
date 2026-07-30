# MonarchAegis Deployment Script

# 1. Get the current active Git Branch
$branch = git branch --show-current
if ($LASTEXITCODE -ne 0) {
    Write-Host "Error: Not a valid git repository or could not detect branch." -ForegroundColor Red
    exit 1
}

Write-Host "Detected branch: $branch" -ForegroundColor Cyan

# 2. Determine Docker Tags based on the branch
$tags = @()
if ($branch -eq "main") {
    Write-Host "Pushing Production Release to GitHub and Docker Hub (latest, stable)" -ForegroundColor Green
    $tags += "cthexiv/monarchaegis:latest"
    $tags += "cthexiv/monarchaegis:stable"
}
elseif ($branch -eq "develop") {
    Write-Host "Pushing Nightly Build to GitHub and Docker Hub (nightly)" -ForegroundColor Yellow
    $tags += "cthexiv/monarchaegis:nightly"
}
else {
    Write-Host "Error: You must be on 'main' or 'develop' to deploy." -ForegroundColor Red
    exit 1
}

# 3. Push source code to GitHub
Write-Host "`n[1/2] Pushing code to GitHub origin/$branch..." -ForegroundColor Cyan
git push origin $branch
if ($LASTEXITCODE -ne 0) {
    Write-Host "Failed to push to GitHub. Aborting deployment." -ForegroundColor Red
    exit 1
}

# 4. Build Docker Image
Write-Host "`n[2/2] Building and Pushing Docker Image..." -ForegroundColor Cyan
$buildArgs = @("build", "--no-cache")
foreach ($tag in $tags) {
    $buildArgs += "-t"
    $buildArgs += $tag
}
$buildArgs += "."

# Execute docker build
& docker $buildArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host "Docker build failed." -ForegroundColor Red
    exit 1
}

# 5. Push Docker tags
foreach ($tag in $tags) {
    Write-Host "Pushing $tag to Docker Hub..." -ForegroundColor Cyan
    docker push $tag
}

Write-Host "`nDeployment Complete!" -ForegroundColor Green
