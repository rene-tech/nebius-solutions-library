"""Public chemical structures for computational checks, not safety assertions."""

MOLECULES = [
    {"id": "aspirin", "smiles": "CC(=O)Oc1ccccc1C(=O)O"},
    {"id": "caffeine", "smiles": "Cn1c(=O)c2c(ncn2C)n(C)c1=O"},
    {"id": "acetaminophen", "smiles": "CC(=O)Nc1ccc(O)cc1"},
    {"id": "ibuprofen", "smiles": "CC(C)Cc1ccc(C(C)C(=O)O)cc1"},
    {"id": "lidocaine", "smiles": "CCN(CC)CC(=O)Nc1c(C)cccc1C"},
    {"id": "diphenhydramine", "smiles": "CN(C)CCOC(c1ccccc1)c1ccccc1"},
    {"id": "propranolol", "smiles": "CC(C)NCC(O)COc1cccc2ccccc12"},
    {"id": "nicotine", "smiles": "CN1CCC[C@H]1c1cccnc1"},
    {"id": "alanine", "smiles": "N[C@@H](C)C(=O)O"},
    {"id": "glycine", "smiles": "NCC(=O)O"},
    {"id": "glucose", "smiles": "O=C[C@H](O)[C@@H](O)[C@H](O)[C@H](O)CO"},
    {"id": "ethanol", "smiles": "CCO"},
]


def semantic_requests():
    return [{"molecules": MOLECULES[:6]}, {"molecules": MOLECULES[6:]}]
