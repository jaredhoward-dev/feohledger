# Backup erasure (.github/workflows/backup-erasure.yml, docs/decisions.md §490).
#
# `/legal/dpa` § 13 promises a removed tenant's backup dumps go within 60 days.
# The principle this file encodes: nothing the VM can trigger on its own may
# delete a backup. The VM role (compute.tf, Sid `Backups`) stays unable to
# delete from the backup bucket, so a compromised VM cannot wipe the backup
# history. Deletion runs in a GitHub Actions job that a human approves, under a
# role only that job can assume:
#
#   feohledger-backup-erasure-check  the daily `check` job, no environment,
#       trusted for refs/heads/main only. May run ONE document on the VM: the
#       read-only `summary`, which prints counts. No S3 at all.
#   feohledger-backup-erasure        the `erase` job, in its own
#       `backup-erasure` environment (required reviewer, `main` only), trusted
#       for that environment only. Not `production`: aws-deploy.yml and
#       aws-oidc-preflight.yml run in it and can be dispatched from any branch,
#       so its subject would let any approved run of them assume this role.
#       Runs the read-only `owed` document and the `mark-cleared` document, may
#       list object versions under `pg/` only, may delete versions of
#       `pg/*/feoh_*.dump` only, and is explicitly denied the shared
#       control-plane dump `feohledger.dump`. No Get, no Put, no DeleteObject
#       (on a versioned bucket that only adds a delete marker).
#
# Like deploy.tf's, every document takes either no parameter or parameters SSM
# itself refuses unless they are a UUID / a run number — holding either role is
# not a shell on the VM.
#
# The backup bucket has no Object Lock (s3.tf) and no MFA delete (its
# versioning block sets none), so a versioned DeleteObject by version id is
# permanent: what this job deletes is gone.

# ── GitHub's OIDC provider ───────────────────────────────────────────────────

# Created by the estate account baseline with the deploy role (README.md § The
# deploy role is not owned here); this module only reads it.
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

locals {
  # The VM checkout and the user every document runs as, as in deploy.tf.
  erasure_queue_sh = "/home/${local.deploy_user}/feohledger/deploy/backup-erasure-queue.sh"

  # The two subjects. The prefix is a variable, never built from the repo slug:
  # this repo presents GitHub's immutable subject claims (README.md § The deploy
  # role is not owned here, the #449 gotcha), and a wrong subject fails silently
  # at the first AssumeRoleWithWebIdentity.
  backup_erasure_check_sub = "${var.github_oidc_sub_prefix}:ref:refs/heads/main"
  backup_erasure_erase_sub = "${var.github_oidc_sub_prefix}:environment:backup-erasure"

  # Every per-tenant dump, and the one dump no erasure may touch. The `_` is
  # what keeps the first from matching the second; the Deny below does not rely
  # on it.
  # The check job's heartbeat (CheckRan) and overdue count (Overdue): the
  # alarms below catch a schedule GitHub disabled or a workflow that stopped
  # running, which would otherwise silence the 45-day alarm with no signal.
  backup_erasure_metric_namespace = "FeohLedger/BackupErasure"

  backup_tenant_dumps_arn  = "${aws_s3_bucket.backups.arn}/pg/*/feoh_*.dump"
  backup_control_dumps_arn = "${aws_s3_bucket.backups.arn}/pg/*/feohledger.dump"
}

# ── Command documents ────────────────────────────────────────────────────────

resource "aws_ssm_document" "backup_erasure_summary" {
  name            = "${var.project}-backup-erasure-summary"
  document_type   = "Command"
  document_format = "JSON"

  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Count the removed workspaces still owed a backup erasure. Read-only; prints counts, no identifiers."
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "summary"
      inputs = {
        timeoutSeconds = "300"
        runCommand     = ["sudo -iu ${local.deploy_user} ${local.erasure_queue_sh} summary"]
      }
    }]
  })
}

resource "aws_ssm_document" "backup_erasure_owed" {
  name            = "${var.project}-backup-erasure-owed"
  document_type   = "Command"
  document_format = "JSON"

  content = jsonencode({
    schemaVersion = "2.2"
    description   = "List the removed workspaces still owed a backup erasure, with their slugs. Read-only."
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "owed"
      inputs = {
        timeoutSeconds = "300"
        runCommand     = ["sudo -iu ${local.deploy_user} ${local.erasure_queue_sh} owed"]
      }
    }]
  })
}

resource "aws_ssm_document" "backup_erasure_mark_cleared" {
  name            = "${var.project}-backup-erasure-mark-cleared"
  document_type   = "Command"
  document_format = "JSON"

  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Record that one removed workspace's backups are erased, by the GitHub run that erased them."
    parameters = {
      OrganizationId = {
        type        = "String"
        description = "The removed organisation's id, as the owed document listed it."
        # SSM rejects anything else at SendCommand time, before a shell
        # exists — the same guard as deploy.tf's Ref.
        allowedPattern = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
      }
      RunId = {
        type           = "String"
        description    = "The GitHub Actions run id, recorded as github-actions-run-<id>."
        allowedPattern = "^[0-9]{1,20}$"
      }
    }
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "markCleared"
      inputs = {
        timeoutSeconds = "300"
        runCommand     = ["sudo -iu ${local.deploy_user} ${local.erasure_queue_sh} mark-cleared '{{ OrganizationId }}' '{{ RunId }}'"]
      }
    }]
  })
}

# ── Trust ────────────────────────────────────────────────────────────────────

data "aws_iam_policy_document" "backup_erasure_check_trust" {
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
    # StringEquals, one value: the scheduled / dispatched run of main and
    # nothing else — not a pull request, not another branch, not an
    # environment job.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [local.backup_erasure_check_sub]
    }
  }
}

data "aws_iam_policy_document" "backup_erasure_trust" {
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
    # Only a job in the dedicated `backup-erasure` environment, which waits
    # for its required reviewer before it gets a token at all and which only
    # `main` may deploy to (both owner settings, README.md § Backup erasure).
    # Never `production`: other workflows share it.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [local.backup_erasure_erase_sub]
    }
    # And only from main. An environment job's `sub` names the environment,
    # not the branch, so this is the AWS-side half of the environment's
    # main-only branch rule (an owner setting the job also reads back before
    # it asks for a token). `ref` is a documented GitHub condition key for
    # AssumeRoleWithWebIdentity (IAM User Guide, condition keys for OIDC).
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:ref"
      values   = ["refs/heads/main"]
    }
  }
}

resource "aws_iam_role" "backup_erasure_check" {
  name                 = "${var.project}-backup-erasure-check"
  description          = "backup-erasure.yml check job: count the owed-erasure queue on the VM. No S3."
  assume_role_policy   = data.aws_iam_policy_document.backup_erasure_check_trust.json
  max_session_duration = 3600
}

resource "aws_iam_role" "backup_erasure" {
  name                 = "${var.project}-backup-erasure"
  description          = "backup-erasure.yml erase job (backup-erasure environment, approved): delete removed tenants' backup dump versions."
  assume_role_policy   = data.aws_iam_policy_document.backup_erasure_trust.json
  max_session_duration = 3600
}

# ── Permissions ──────────────────────────────────────────────────────────────

data "aws_iam_policy_document" "backup_erasure_check" {
  # SendCommand is authorized against the instance AND the document, so naming
  # only these keeps AWS-RunShellScript and every other document out of reach.
  statement {
    sid       = "RunTheSummaryDocumentOnTheAppVm"
    actions   = ["ssm:SendCommand"]
    resources = [aws_instance.app.arn, aws_ssm_document.backup_erasure_summary.arn]
  }

  # No resource type exists for GetCommandInvocation (deploy.tf has the
  # reasoning); it reads a command whose id the caller already holds. Accepted
  # for both roles (decisions §490): ids are not guessable and neither role
  # can list commands.
  statement {
    sid       = "ReadTheCommandResult"
    actions   = ["ssm:GetCommandInvocation"]
    resources = ["*"]
  }
}

data "aws_iam_policy_document" "backup_erasure_check_metrics" {
  # PutMetricData has no resource type; the namespace condition key is what
  # scopes it, so the role can write this workflow's heartbeat and nothing
  # else a CloudWatch alarm reads.
  statement {
    sid       = "PublishTheErasureHeartbeat"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = [local.backup_erasure_metric_namespace]
    }
  }
}

resource "aws_iam_role_policy" "backup_erasure_check_metrics" {
  name   = "backup-erasure-check-metrics"
  role   = aws_iam_role.backup_erasure_check.name
  policy = data.aws_iam_policy_document.backup_erasure_check_metrics.json
}

resource "aws_iam_role_policy" "backup_erasure_check" {
  name   = "backup-erasure-check"
  role   = aws_iam_role.backup_erasure_check.name
  policy = data.aws_iam_policy_document.backup_erasure_check.json
}

data "aws_iam_policy_document" "backup_erasure" {
  statement {
    sid     = "RunTheQueueDocumentsOnTheAppVm"
    actions = ["ssm:SendCommand"]
    resources = [
      aws_instance.app.arn,
      aws_ssm_document.backup_erasure_owed.arn,
      aws_ssm_document.backup_erasure_mark_cleared.arn,
    ]
  }

  statement {
    sid       = "ReadTheCommandResult"
    actions   = ["ssm:GetCommandInvocation"]
    resources = ["*"]
  }

  # ListObjectVersions is authorized as s3:ListBucketVersions on the bucket,
  # and supports the s3:prefix condition key. Exactly `pg/`: the dump tree,
  # which is the only prefix the job lists.
  statement {
    sid       = "ListDumpVersions"
    actions   = ["s3:ListBucketVersions"]
    resources = [aws_s3_bucket.backups.arn]
    condition {
      test     = "StringEquals"
      variable = "s3:prefix"
      values   = ["pg/"]
    }
  }

  # Belt and braces for the Allow above: a request with any other prefix, or
  # none (a negated operator matches when the key is absent), is refused
  # outright rather than merely not allowed.
  statement {
    sid       = "DenyListingOutsideTheDumps"
    effect    = "Deny"
    actions   = ["s3:ListBucketVersions"]
    resources = [aws_s3_bucket.backups.arn]
    condition {
      test     = "StringNotEquals"
      variable = "s3:prefix"
      values   = ["pg/"]
    }
  }

  # A permanent delete of one version (or delete marker) of a per-tenant dump.
  # Not s3:DeleteObject: on a versioned bucket that only hides the object
  # behind a delete marker, which erases nothing.
  statement {
    sid       = "DeleteTenantDumpVersions"
    actions   = ["s3:DeleteObjectVersion"]
    resources = [local.backup_tenant_dumps_arn]
  }

  # The shared control-plane dump holds every tenant's control-plane rows. It
  # is never erased per tenant (the DPA discloses it ages out instead), and
  # this Deny holds even if the Allow above is ever widened.
  statement {
    sid       = "NeverTheControlPlaneDump"
    effect    = "Deny"
    actions   = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
    resources = [local.backup_control_dumps_arn]
  }
}

resource "aws_iam_role_policy" "backup_erasure" {
  name   = "backup-erasure"
  role   = aws_iam_role.backup_erasure.name
  policy = data.aws_iam_policy_document.backup_erasure.json
}

# ── Alarms: the erasure must not stop silently ──────────────────────────────
#
# GitHub disables scheduled workflows in a public repo after 60 days without
# repository activity, and a broken or disarmed workflow also stops the daily
# check — and with it the red run that flags an erasure 45 days old. These two
# alarms watch from AWS instead. Off until var.backup_erasure_alarms_enabled:
# armed before the workflow has run once, the heartbeat alarm would page
# about a check that was never meant to run yet (README.md § Backup erasure).

resource "aws_cloudwatch_metric_alarm" "backup_erasure_heartbeat" {
  count = var.backup_erasure_alarms_enabled ? 1 : 0

  alarm_name          = "${var.project}-${var.environment}-backup-erasure-heartbeat"
  alarm_description   = "The backup-erasure workflow's daily check has not run for two days: GitHub may have disabled the schedule (60 days without repository activity), the BACKUP_ERASURE_ENABLED variable or a secret changed, or the check is failing. Until it runs, nothing warns before the DPA's 60-day backup deletion deadline. Re-enable / fix .github/workflows/backup-erasure.yml. docs/founder-runbooks/workspace-closure.md."
  namespace           = local.backup_erasure_metric_namespace
  metric_name         = "CheckRan"
  statistic           = "Sum"
  period              = 86400
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  # No datapoint is exactly the failure this alarm exists for.
  treat_missing_data = "breaching"
  alarm_actions      = [aws_sns_topic.alerts.arn]
  ok_actions         = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "backup_erasure_overdue" {
  count = var.backup_erasure_alarms_enabled ? 1 : 0

  alarm_name          = "${var.project}-${var.environment}-backup-erasure-overdue"
  alarm_description   = "A removed workspace's backups have been owed for 45+ days; /legal/dpa § 13 allows 60. Approve the pending erase job of .github/workflows/backup-erasure.yml, or see docs/founder-runbooks/workspace-closure.md."
  namespace           = local.backup_erasure_metric_namespace
  metric_name         = "Overdue"
  statistic           = "Maximum"
  period              = 86400
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  # A missing day is the heartbeat alarm's to report.
  treat_missing_data = "notBreaching"
  alarm_actions      = [aws_sns_topic.alerts.arn]
  ok_actions         = [aws_sns_topic.alerts.arn]
}
