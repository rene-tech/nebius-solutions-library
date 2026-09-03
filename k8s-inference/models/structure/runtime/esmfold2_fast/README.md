# ESMFold2-Fast adapter

`esmfold2-fast` is a separate distilled identity. It shares the canonical
scientific envelope and ESMC/CCD dependencies with the full model, but rejects
every MSA mode rather than ignoring it. The controller schedules CPU
normalization before the GPU stage and fixes production defaults at 20 recycles
and 200 sampling steps.
