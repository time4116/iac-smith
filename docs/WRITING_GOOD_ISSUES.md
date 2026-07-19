# Writing good IaC Smith issues

IaC Smith turns a GitHub issue into a Terraform/Terragrunt pull request. It reads the issue **literally** — there is no human in the loop to infer what you "obviously" meant. A schema-valid, plan-clean PR can still be dead at runtime if the issue left a load-bearing requirement unstated. This guide captures the pitfalls that have actually bitten us and what a good issue looks like.

## The case that motivated this guide

A showcase issue asked for a *"public-facing nginx service on ECS Fargate"* with *"Public IP/DNS exposure"*, consuming an existing `foundation` networking stack. The generated stack passed every gate and merged — and would have been dead at runtime. The Fargate tasks were placed in the foundation's **private** subnets with `assign_public_ip = false`, and that foundation deliberately had **no NAT gateway**. Result: the tasks had no path to the internet to pull the `nginx` image or reach CloudWatch Logs.

Nothing was "wrong" in a way a schema or a plan could see. The gap was entirely in what the issue did and didn't pin down. Two lessons fall out of it.

## Pitfall 1 — Ingress and egress are different requirements

*"Public-facing"* and *"public IP/DNS exposure"* describe **ingress**: how users reach the service. A public load balancer satisfies that completely — the service is reachable — while the tasks behind it may still have **zero egress** (no outbound path to pull images, ship logs, or call external APIs).

The two are independent axes, and an issue that only mentions ingress leaves egress to chance. If the workload needs to reach *out*, say so:

> Tasks must have outbound internet access to pull the container image and deliver logs to CloudWatch.

## Pitfall 2 — Cross-stack assumptions are invisible

When an issue consumes an existing stack (*"wire into the `foundation` networking"*), the workload inherits whatever that stack actually provides — including what it **doesn't**. The foundation here intentionally omitted a NAT gateway and documented that *"workloads needing outbound internet run in the public subnets."* The workload issue never restated that, and the composition quietly assumed the conventional "Fargate tasks live in private subnets" pattern, which only works when a NAT exists.

If you depend on a stack, state what you expect from it, and know its limits:

> Consume `foundation`'s **public** subnet outputs; the foundation has no NAT, so egress workloads must run in the public subnets with public IPs assigned.

Better still, fix it at the source: a foundation meant to host egress-needing workloads should *provide* egress (a NAT gateway or VPC endpoints) so workloads don't each have to work around its absence.

## Pitfall 3 — Describe outcomes *and* the mechanism where it matters

The tool translates intent into resources, so you generally describe *what*, not *how*. But when the "how" is load-bearing and non-obvious — subnet placement, whether a task gets a public IP, which egress path it uses — name it. *"Public-facing"* is an outcome with more than one valid implementation; *"tasks in public subnets with public IPs assigned"* is unambiguous.

## Pitfall 4 — Pin versions; avoid `latest`

An issue that says *"use the latest nginx image"* produces an unpinned (`:latest`) reference — non-reproducible and a moving target across applies. Pin an explicit tag or digest (`nginx:1.27.4`) unless you have a specific reason not to.

## What a good issue looks like

A good issue states, in plain language:

- **The workload and its shape** — what to deploy, launch type, how users reach it.
- **Environment and region** — e.g. non-prod only, `us-west-2`.
- **Ingress** — how the service is reached (ALB, public IP, DNS).
- **Egress** — whether the workload needs outbound internet, and for what (image pulls, logging, external services). Do not assume it "just works."
- **Networking placement, when it matters** — public vs private subnets, public IP or not — rather than only the outcome.
- **Dependencies** — which existing stacks to consume, what outputs you expect, and any known limits of those stacks (e.g. "foundation has no NAT").
- **Pinned versions** — image tags/digests, not `latest`.

### Example

> Deploy a public-facing nginx service on ECS Fargate in **non-prod**, `us-west-2`.
>
> - **Ingress:** behind an internet-facing Application Load Balancer in the foundation's public subnets.
> - **Placement + egress:** run the Fargate tasks in the foundation's **public** subnets with `assign_public_ip = true`. The `foundation` stack has **no NAT gateway**, so this is the only egress path for pulling the image and delivering logs.
> - **Dependency:** consume `foundation` (`vpc_id`, `public_subnets`).
> - **Image:** `nginx:1.27.4` (pinned).

Every requirement that could otherwise be silently assumed is on the page. That is the difference between a PR that plans cleanly and a PR that actually runs.
