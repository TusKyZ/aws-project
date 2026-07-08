# GitHub Actions OIDC federation: CI mints short-lived credentials per run by
# assuming these roles — no long-lived AWS keys exist in the repository, its
# secrets, or anywhere else.

data "aws_caller_identity" "current" {}

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]

  # AWS has trusted GitHub's OIDC root CA directly since July 2023, so these
  # thumbprints are not used for validation — the API still requires the field.
  thumbprint_list = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f2df264fcd",
  ]
}

locals {
  oidc_sub_prefix = "repo:${var.github_repository}"
}

# --- Plan role: read-only, assumable from pull_request runs only -------------
# ReadOnlyAccess covers every describe/list/get the plan needs, including the
# state object in S3, and deliberately excludes secretsmanager:GetSecretValue —
# a malicious PR cannot exfiltrate the API key by editing the workflow.
# Plans run with -lock=false so the role needs no state-write permission.

data "aws_iam_policy_document" "plan_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["${local.oidc_sub_prefix}:pull_request"]
    }
  }
}

resource "aws_iam_role" "plan" {
  name                 = "sentinel-ci-plan"
  assume_role_policy   = data.aws_iam_policy_document.plan_trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy_attachment" "plan_readonly" {
  role       = aws_iam_role.plan.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

# --- Apply role: deploys the stack, assumable only via the gated environment -
# The sub claim `environment:<name>` is only minted for workflow runs that
# passed that GitHub environment's protection rules (required reviewers) —
# the human approval IS the security boundary.

data "aws_iam_policy_document" "apply_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["${local.oidc_sub_prefix}:environment:${var.apply_environment}"]
    }
  }
}

resource "aws_iam_role" "apply" {
  name                 = "sentinel-ci-apply"
  assume_role_policy   = data.aws_iam_policy_document.apply_trust.json
  max_session_duration = 3600
}

# AdministratorAccess is a deliberate, documented tradeoff for a solo portfolio
# project: the effective boundary is the trust policy (single repo, single
# gated environment, human approval per apply). Production hardening would
# swap this for a service-scoped policy plus a permissions boundary.
resource "aws_iam_role_policy_attachment" "apply_admin" {
  role       = aws_iam_role.apply.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}
