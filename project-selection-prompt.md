Construct a plausible open-source software stack for a compound AI system.

Organize the stack into four layers:

1. data_pipelines: data acquisition, processing, storage, and retrieval.
2. training: model training, fine-tuning, evaluation, and model management.
3. integration_serving: model integration, inference, orchestration, and guardrails.
4. cross_cutting: operating systems, containers, orchestration, networking,
   monitoring, and other shared infrastructure.

Return a YAML file named stack_config.yaml. For each layer, include a
description and a projects list. For every project, provide:

- repo: GitHub repository as owner/name
- role: the project's function in the stack
- status: "selected" for the baseline stack or "alternative" for a substitute
- languages: the project's primary programming languages
- notes: a short rationale for its inclusion
- usage_ref: a link supporting its use in a production AI or ML setting

Select projects that collectively cover the functional roles of a compound
AI system. Include plausible alternatives where useful. Use the existing
stack_config.yaml as the formatting template.

Do not invent references. If you cannot verify a reference, mark it as
unverified. Return only the YAML and a separate list of references that
require verification.
