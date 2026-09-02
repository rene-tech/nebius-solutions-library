import copy, json, pathlib, unittest, importlib.util
ROOT=pathlib.Path(__file__).parents[1]
spec=importlib.util.spec_from_file_location('contract',ROOT/'validate_contract.py'); contract=importlib.util.module_from_spec(spec); spec.loader.exec_module(contract)
class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m=json.loads((ROOT/'qualification-matrix.json').read_text()); cls.f=json.loads((ROOT/'semantic-fixtures.json').read_text())
    def test_documents(self): self.assertEqual(contract.validate_documents(self.m,self.f),[])
    def test_local_nvme_rejected(self):
        m=copy.deepcopy(self.m); m['target']['local_nvme_model_cache_available']=True; self.assertTrue(contract.validate_documents(m,self.f))
    def test_duplicate_partition_rejected(self):
        m=copy.deepcopy(self.m); m['tracks'][1]['runtime_units'][0]['evidence_partition_key']=m['tracks'][0]['runtime_units'][0]['evidence_partition_key']; self.assertTrue(contract.validate_documents(m,self.f))
    def test_snapshot_backend_must_be_explicit(self):
        m=copy.deepcopy(self.m); m['tracks'][0]['runtime_units'][0]['snapshot']['backend']='criu'; self.assertTrue(contract.validate_documents(m,self.f))
if __name__ == '__main__': unittest.main()
