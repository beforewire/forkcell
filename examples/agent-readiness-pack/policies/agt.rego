package beforewire.agent_gate

import rego.v1

# AGT/ACS Rego policy for local shadow readiness packets.
# The AGT manifest selects the packet ActionIntent as input.policy_target.value.
# Verdicts use AGT/ACS decisions: allow, deny, or escalate. The packet harness
# maps escalate to the BeforeWire "ask" effect used for approval waits.

intent := object.get(input.policy_target, "value", {})
argv := object.get(intent, "argv", [])
primitive := object.get(intent, "primitive", "")
resource := object.get(intent, "resource", "")
path := object.get(intent, "path", object.get(intent, "source_path", ""))
sink_host := object.get(intent, "sink_host", "")
tool_description := lower(object.get(intent, "tool_description", ""))
baseline_hash := object.get(intent, "baseline_hash", "")
current_hash := object.get(intent, "current_hash", "")
dry_run := object.get(intent, "dry_run", false)

default effect := "deny"

secret_path if {
	resource == "filesystem:workspace:.env"
}

secret_path if {
	path == ".env"
}

ci_workflow_path if {
	startswith(resource, "filesystem:workspace:.github/workflows/")
}

ci_workflow_path if {
	startswith(path, ".github/workflows/")
}

package_manifest_path if {
	resource == "filesystem:workspace:package.json"
}

package_manifest_path if {
	path == "package.json"
}

dangerous_shell if {
	primitive == "shell.run"
	some i
	contains(lower(argv[i]), "curl")
	some j
	contains(lower(argv[j]), "sh")
}

dangerous_shell if {
	primitive == "shell.run"
	some i
	argv[i] == "-c"
}

mcp_poisoned if {
	primitive == "mcp.tool_call"
	contains(tool_description, "ignore previous rules")
}

mcp_poisoned if {
	primitive == "mcp.tool_call"
	contains(tool_description, "environment variables")
}

mcp_drift if {
	primitive == "mcp.tool_call"
	baseline_hash != ""
	current_hash != ""
	baseline_hash != current_hash
}

unknown_egress if {
	primitive == "http.call"
	sink_host != "api.github.com"
}

deny_reasons contains "secret_path" if secret_path
deny_reasons contains "unknown_egress" if unknown_egress
deny_reasons contains "dangerous_shell" if dangerous_shell
deny_reasons contains "mcp_poisoned" if mcp_poisoned
deny_reasons contains "receipt_tamper" if primitive == "receipt.verify"

ask_reasons contains "ci_workflow_path" if ci_workflow_path
ask_reasons contains "package_manifest_path" if package_manifest_path
ask_reasons contains "mcp_drift" if mcp_drift

allow_action if {
	primitive == "filesystem.read"
	not secret_path
}

allow_action if {
	primitive == "shell.run"
	argv == ["git", "status", "--short"]
}

allow_action if {
	primitive == "broker.commit"
	dry_run == true
}

effect := "deny" if {
	count(deny_reasons) > 0
} else := "ask" if {
	count(ask_reasons) > 0
} else := "allow" if {
	allow_action
}

decision := "escalate" if {
	effect == "ask"
} else := effect

matched_rules := deny_reasons | ask_reasons

reason := "policy_default_deny" if {
	count(matched_rules) == 0
	effect == "deny"
} else := concat(",", sort(matched_rules))

verdict := {
	"decision": decision,
	"reason": reason,
	"message": sprintf("BeforeWire AGT shadow govern decision: %s", [effect]),
}
