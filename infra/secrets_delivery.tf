# Secrets delivery (issue #735, docs/decisions.md §591).
#
# The production secrets file is sops ciphertext kept in the private
# infra-secrets repo (feohledger/prod.sops.yaml). On every merge to that repo's
# `main` touching the file, its own CI uploads the CIPHERTEXT to the dedicated
# bucket below; every gated deploy (vm-deploy.yml → deploy.sh) fetches the
# current version onto the VM before decrypt-env.sh. Plaintext exists only on
# the VM, and the `production` reviewer still stands between a secrets change
# and deploy/.env: nothing here applies a file, it only makes the next approved
# deploy see it.
#
# Two principals, one key, nothing else:
#
#   feohledger-secrets-upload  infra-secrets' CI, trusted for that repo's
#       `main` only. May PutObject on the one key. No Get (it confirms the
#       upload by the checksum S3 verifies and returns on the Put itself), no
#       delete, and NO KMS at all: without the sops key it cannot forge a file
#       sops would accept (the MAC), only replay an older genuine one — which
#       deploy.sh's replay guard refuses (lib.sh feoh_fetch_secrets).
#   the app VM role (compute.tf)  may read the one key; nothing may write it.
#
# The bucket is SSE-S3, not the app KMS key: S3 checks the uploader's access
# to an SSE-KMS key on every Put, and the uploader is meant to hold no KMS
# permission. The object is sops ciphertext already; SSE-S3 is the at-rest
# baseline on top of it.
#
# Its name is var.secrets_bucket_name, set in the private prod.tfvars.sops like
# every other bucket's: this repo is public.

locals {
  # The one object. The bucket holds nothing else, so no project prefix.
  secrets_object_key = "prod.sops.yaml"

  # infra-secrets' `main`, on that repo's own subject prefix (read it, never
  # build it from the slug — README.md § The deploy role is not owned here).
  secrets_upload_sub = "${var.infra_secrets_oidc_sub_prefix}:ref:refs/heads/main"

  secrets_object_arn = "${aws_s3_bucket.secrets.arn}/${local.secrets_object_key}"
}

# ── Bucket ───────────────────────────────────────────────────────────────────

resource "aws_s3_bucket" "secrets" {
  bucket = var.secrets_bucket_name

  tags = {
    Name    = var.secrets_bucket_name
    purpose = "sops-ciphertext"
  }
}

# Every upload is a version: the deploy log names the one it applied, and an
# older one is what a deliberate rollback re-uploads.
resource "aws_s3_bucket_versioning" "secrets" {
  bucket = aws_s3_bucket.secrets.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "secrets" {
  bucket = aws_s3_bucket.secrets.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "secrets" {
  bucket                  = aws_s3_bucket.secrets.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "secrets" {
  bucket = aws_s3_bucket.secrets.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_logging" "secrets" {
  bucket        = aws_s3_bucket.secrets.id
  target_bucket = aws_s3_bucket.access_logs.id
  target_prefix = "secrets/"

  depends_on = [
    aws_s3_bucket_policy.access_logs_delivery,
    aws_s3_bucket_server_side_encryption_configuration.access_logs,
  ]
}

# About 30 superseded versions, then they go: enough history to see what
# changed and roll back, without keeping every key that was ever rotated out.
resource "aws_s3_bucket_lifecycle_configuration" "secrets" {
  bucket = aws_s3_bucket.secrets.id

  rule {
    id     = "keep-30-noncurrent-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days           = 1
      newer_noncurrent_versions = 30
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

# Resource-side half of the two grants. The identity policies below and in
# compute.tf ALLOW; these DENY everyone else, so a future broad grant elsewhere
# in the account (an admin role's s3:*) cannot quietly become a second writer
# or reader. Bucket-level administration (this policy itself) is untouched.
data "aws_iam_policy_document" "secrets_bucket" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.secrets.arn,
      "${aws_s3_bucket.secrets.arn}/*",
    ]
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid       = "OnlyTheUploaderWrites"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.secrets.arn}/*"]
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    condition {
      test     = "ArnNotEquals"
      variable = "aws:PrincipalArn"
      values   = [aws_iam_role.secrets_upload.arn]
    }
  }

  statement {
    sid       = "OnlyTheVmReads"
    effect    = "Deny"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${aws_s3_bucket.secrets.arn}/*"]
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    condition {
      test     = "ArnNotEquals"
      variable = "aws:PrincipalArn"
      values   = [aws_iam_role.app.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "secrets" {
  bucket = aws_s3_bucket.secrets.id
  policy = data.aws_iam_policy_document.secrets_bucket.json

  depends_on = [aws_s3_bucket_public_access_block.secrets]
}

# ── The infra-secrets CI role ────────────────────────────────────────────────

data "aws_iam_policy_document" "secrets_upload_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    # StringEquals, one value: a push to infra-secrets' main. Not a pull
    # request, not another branch. That repo is on a plan with no environments
    # or branch protection, so nothing gates this job there — which is why the
    # role can only put ciphertext, and the gate stays in this repo's deploy.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [local.secrets_upload_sub]
    }
  }
}

resource "aws_iam_role" "secrets_upload" {
  name                 = "${var.project}-secrets-upload"
  description          = "infra-secrets CI: put the sops ciphertext of the production secrets file. No read, no KMS."
  assume_role_policy   = data.aws_iam_policy_document.secrets_upload_trust.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "secrets_upload" {
  statement {
    sid       = "PutTheCiphertext"
    actions   = ["s3:PutObject"]
    resources = [local.secrets_object_arn]
  }
}

resource "aws_iam_role_policy" "secrets_upload" {
  name   = "secrets-upload"
  role   = aws_iam_role.secrets_upload.id
  policy = data.aws_iam_policy_document.secrets_upload.json
}
