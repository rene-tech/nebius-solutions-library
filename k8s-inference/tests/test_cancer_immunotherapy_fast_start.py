import importlib.util, pathlib
p=pathlib.Path(__file__).parents[1]/'models/cancer-immunotherapy-fast-start/tests/test_contract.py'
s=importlib.util.spec_from_file_location('cancer_fast_start_contract',p); m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
ContractTests=m.ContractTests
