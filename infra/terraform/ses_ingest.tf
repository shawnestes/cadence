# SES → SNS → SQS ingest pipeline for Cadence contact events.
#
# D6: This file is a plan only — do not run terraform apply without explicit
# confirmation.  No provider block is included; callers supply it via a root
# module or -var-file.
#
# Required provider: hashicorp/aws ~> 5.0

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------

variable "env" {
  description = "Deployment environment (dev / staging / prod)"
  type        = string
  default     = "dev"
}

# ---------------------------------------------------------------------------
# SES Configuration Set
# ---------------------------------------------------------------------------

resource "aws_ses_configuration_set" "cadence_events" {
  name = "cadence_events"

  reputation_metrics_enabled = true
  sending_enabled            = true
}

# ---------------------------------------------------------------------------
# SNS Topic — receives SES event notifications
# ---------------------------------------------------------------------------

resource "aws_sns_topic" "ses_events" {
  name              = "cadence-ses-events-${var.env}"
  kms_master_key_id = "alias/aws/sns"
}

# ---------------------------------------------------------------------------
# SQS Dead-Letter Queue
# ---------------------------------------------------------------------------

resource "aws_sqs_queue" "ses_events_dlq" {
  name                      = "cadence-ses-events-dlq-${var.env}"
  message_retention_seconds = 1209600 # 14 days

  kms_master_key_id                 = "alias/aws/sqs"
  kms_data_key_reuse_period_seconds = 300
}

# ---------------------------------------------------------------------------
# SQS Main Queue
# ---------------------------------------------------------------------------

resource "aws_sqs_queue" "ses_events" {
  name                       = "cadence-ses-events-${var.env}"
  visibility_timeout_seconds = 30
  message_retention_seconds  = 86400 # 24 hours
  receive_wait_time_seconds  = 20    # long-polling

  kms_master_key_id                 = "alias/aws/sqs"
  kms_data_key_reuse_period_seconds = 300

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.ses_events_dlq.arn
    maxReceiveCount     = 5
  })
}

# ---------------------------------------------------------------------------
# SQS Queue Policy — allow SNS to publish to the queue
# ---------------------------------------------------------------------------

resource "aws_sqs_queue_policy" "ses_events" {
  queue_url = aws_sqs_queue.ses_events.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowSNSPublish"
        Effect = "Allow"
        Principal = {
          Service = "sns.amazonaws.com"
        }
        Action   = "sqs:SendMessage"
        Resource = aws_sqs_queue.ses_events.arn
        Condition = {
          ArnEquals = {
            "aws:SourceArn" = aws_sns_topic.ses_events.arn
          }
        }
      }
    ]
  })
}

# ---------------------------------------------------------------------------
# SNS → SQS Subscription
# raw_message_delivery=false preserves the SNS envelope for signature
# verification on ingest (D6 / SPEC §5).
# ---------------------------------------------------------------------------

resource "aws_sns_topic_subscription" "sqs" {
  topic_arn            = aws_sns_topic.ses_events.arn
  protocol             = "sqs"
  endpoint             = aws_sqs_queue.ses_events.arn
  raw_message_delivery = false
}

# ---------------------------------------------------------------------------
# SES Event Destination — fan all event types to the SNS topic
# ---------------------------------------------------------------------------

resource "aws_ses_configuration_set_event_destination" "to_sns" {
  configuration_set_name = aws_ses_configuration_set.cadence_events.name
  event_destination_name = "to_sns"
  enabled                = true

  matching_types = [
    "send",
    "delivery",
    "bounce",
    "complaint",
    "reject",
    "open",
    "click",
    "renderingFailure",
    "deliveryDelay",
    "subscription",
  ]

  sns_destination {
    topic_arn = aws_sns_topic.ses_events.arn
  }
}

# ---------------------------------------------------------------------------
# IAM Policy — SQS poller permissions
# ---------------------------------------------------------------------------

resource "aws_iam_policy" "sqs_poller" {
  name        = "cadence-sqs-poller-${var.env}"
  description = "Allows the Cadence SQS poller to consume messages from the SES events queue"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SQSPollerAccess"
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
        ]
        Resource = aws_sqs_queue.ses_events.arn
      }
    ]
  })
}

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

output "sqs_event_queue_url" {
  description = "URL of the SES events SQS queue consumed by the Cadence poller"
  value       = aws_sqs_queue.ses_events.url
}
