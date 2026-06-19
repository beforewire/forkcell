# Security Policy

ForkCell `v0.1.0-preview` is experimental.

## Supported Versions

| Version | Supported |
| --- | --- |
| `0.1.0-preview` | Preview support only |

## Security Boundary

ForkCell is not a VM or MicroVM isolation layer. It uses OpenShell for sandbox, process, filesystem, network, egress, credential, and OCSF runtime enforcement. ForkCell adds checkpoint/restore, policy binding, receipts, and decision artifacts.

Do not treat ForkCell as a replacement for production isolation review. Do not store real secrets in workspaces, checkpoints, receipts, logs, or evidence artifacts unless an explicit credential grant and redaction path has been reviewed.

## Reporting

Report suspected vulnerabilities privately to the project maintainers. Do not open public issues containing exploit details, credentials, private keys, customer data, or local machine artifacts.
