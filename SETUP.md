# Judgemind — Setup Checklist

Work through this list in order. Each item should take 5-30 minutes.

## 1. Accounts & Services

- [ ] **Register domain** — judgemind.org (or your preference). Use Cloudflare for DNS.
- [ ] **Create AWS account** — New account, not your personal one.
  - [ ] Enable MFA on root
  - [ ] Create IAM admin user (not root) for daily use
  - [ ] Set billing alerts: $50, $150, $500
  - [ ] Note your preferred region (us-west-2 is the Terraform default)
- [ ] **Create GitHub organization** — `judgemind` (or your preference)
- [ ] **Install GitHub CLI** — `brew install gh` then `gh auth login`
- [ ] **Install Claude Code** — Your primary development tool

## 2. Repository Setup

- [ ] **Create repo** from this bootstrap:
  ```bash
  # From the bootstrap directory
  cd judgemind-bootstrap
  git init
  git add .
  git commit -m "feat: initial repository scaffold"

  # Create the repo on GitHub
  gh repo create judgemind/judgemind --public --source=. --push
  ```

- [ ] **Set up branch protection** on `main`:
  ```bash
  gh api repos/judgemind/judgemind/branches/main/protection \
    -X PUT \
    -f "required_pull_request_reviews[required_approving_review_count]=1" \
    -f "required_status_checks[strict]=true" \
    -f "enforce_admins=false" \
    -f "restrictions=null"
  ```

- [ ] **Update CODEOWNERS** — Replace `YOUR_GITHUB_USERNAME` in `.github/CODEOWNERS`

- [ ] **Create labels**:
  ```bash
  # Run the label creation commands from scripts/seed-issues.md
  ```

- [ ] **Create GitHub Project** board:
  ```bash
  gh project create --title "Judgemind Development" --owner judgemind
  ```
  Then add columns: Backlog, Ready for Agent, In Progress, In Review, Done, Blocked

## 3. AWS Bootstrap

- [ ] **Create Terraform state bucket** (this one is manual — Terraform can't manage its own state bucket):
  ```bash
  aws s3api create-bucket \
    --bucket judgemind-terraform-state \
    --region us-west-2 \
    --create-bucket-configuration LocationConstraint=us-west-2

  aws s3api put-bucket-versioning \
    --bucket judgemind-terraform-state \
    --versioning-configuration Status=Enabled

  aws dynamodb create-table \
    --table-name judgemind-terraform-locks \
    --attribute-definitions AttributeName=LockID,AttributeType=S \
    --key-schema AttributeName=LockID,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --region us-west-2
  ```

- [ ] **Uncomment backend config** in `infra/terraform/main.tf`

- [ ] **Store AWS credentials in GitHub Secrets**:
  ```bash
  gh secret set AWS_ACCESS_KEY_ID
  gh secret set AWS_SECRET_ACCESS_KEY
  gh secret set AWS_REGION --body "us-west-2"
  ```

- [ ] **Deploy S3 document archive**:
  ```bash
  cd infra/terraform
  terraform init
  terraform plan
  terraform apply
  ```

## 4. Create Seed Issues

- [ ] Run the `gh issue create` commands from `scripts/seed-issues.md`
- [ ] Assign priority labels
- [ ] Move P1 issues to "Ready for Agent" on the project board

## 5. Copy Spec Documents

- [ ] Copy your spec docs into `docs/specs/`:
  - `product-spec-v3.md`
  - `architecture-spec-v1.md`
  - `ca-county-investigation.md`
  - `multi-state-investigation.md`
  - `la-court-investigation.md`

## 6. Start Development

- [ ] Open Claude Code in the repo
- [ ] Point it at the first P1 issue
- [ ] Watch it create a branch and start working
- [ ] Review the PR when it's ready

## 7. Start Scraping (URGENT — Do Within Week 2)

Even before the full pipeline is ready:

- [ ] Deploy a minimal scraper to a small EC2 instance or $5 VPS
- [ ] Have it capture raw HTML from LA County tentative rulings page daily
- [ ] Store raw HTML in the S3 document archive bucket
- [ ] This captures data you can never get back. Processing comes later.

---

**Estimated time to complete setup: 2-4 hours**

After setup, your daily time commitment drops to ~1 hour of PR review and decision-making.
