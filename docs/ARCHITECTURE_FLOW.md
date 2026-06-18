# IaC Smith Architecture Flow

This diagram shows the controller flow from issue trigger to reviewable infrastructure pull request. IaC Smith generates and repairs IaC, but it never applies infrastructure directly.

```mermaid
flowchart TD
    A[GitHub issue labeled iac-smith] --> B[GitHub Actions controller workflow]
    B --> C{Owner gated and target repo allowed?}
    C -- No --> Z[Stop without secrets or write access]
    C -- Yes --> D[Assume AWS role with GitHub OIDC]
    D --> E[Fetch source issue and clone target repo]
    E --> F[Scan target repo conventions and snippets]
    F --> G[Infer infrastructure intent with LangGraph]
    G --> H[Plan generated file set]

    H --> I[Bedrock Terraform/Terragrunt generation]
    I --> J[Static review of generated files]
    J -->|Fails| K[Generation repair with exact static errors]
    K --> I
    J -->|Passes| L[Graph-level validation runner]

    L -->|Fails within retry budget| M[Route back through code generation with accumulated errors]
    M --> I
    L -->|Fails after retry budget| N[Block run and report validation errors]
    L -->|Passes| O[Write files into target repo workspace]

    O --> P[Runtime validation]
    P --> P1[terraform fmt and terragrunt hclfmt]
    P1 --> P2[backend-free terraform init and validate where possible]
    P2 --> Q{Runtime validation passed?}

    Q -- No, retries remain --> R[Runtime repair with exact command output]
    R --> O
    Q -- No, retries exhausted --> N
    Q -- Yes --> S[Commit generated IaC to target branch]
    S --> T[Open or update target repo pull request]
    T --> U[Human review and normal GitOps merge/apply path]

    classDef safety fill:#102a43,stroke:#58a6ff,color:#ffffff
    classDef repair fill:#3d2c00,stroke:#d29922,color:#ffffff
    classDef stop fill:#3d0d0d,stroke:#f85149,color:#ffffff

    class C,D,Z,N,U safety
    class K,M,R repair
    class Z,N stop
```

## Control boundaries

- The controller repo orchestrates issue intake, generation, validation, repair, and PR creation.
- The target repo remains the source of truth for Terraform/Terragrunt.
- IaC Smith does not run `terraform apply`. Human review and the target repo's normal GitOps process remain the deployment boundary.
- Repair loops are bounded. If generated IaC cannot be made safe and valid within the retry budget, the controller blocks rather than opening a misleading PR.

## Validation boundaries

Runtime validation is conservative by design. New infrastructure may not have remote state or dependency outputs yet, so IaC Smith focuses on formatting, backend-free initialization, and module-level Terraform validation where possible. Full environment planning remains dependent on target repo state, credentials, and dependency readiness.
