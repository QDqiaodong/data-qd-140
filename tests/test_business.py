
import copy,json,unittest,tempfile,subprocess,sys,hashlib,random,itertools
from pathlib import Path
from fractions import Fraction
from storage_dispatch.model import analyze
ROOT=Path(__file__).resolve().parents[1]
def sample():return json.loads((ROOT/'fixtures/input.json').read_text())
class Business(unittest.TestCase):
    def bad(self,d):
        with self.assertRaises((ValueError,TypeError)):analyze(d)
    def test_unknown_fields(self):
        d=sample();d['unknown']=1;self.bad(d)

    def test_conservation(self):
        r=analyze(sample());self.assertEqual((r['final'],r['grid_in'],r['grid_out'],r['loss'],r['unmet']),('100','175','48','47','97'));self.assertEqual(r['conservation_residual'],'0')
    def test_full_charge(self):
        d=sample();d['initial']=100;d['requests']=[10];r=analyze(d);self.assertEqual(r['grid_in'],'0');self.assertEqual(r['unmet'],'10')
    def test_empty_discharge(self):
        d=sample();d['initial']=0;d['requests']=[-10];r=analyze(d);self.assertEqual(r['grid_out'],'0');self.assertEqual(r['unmet'],'10')
    def test_fraction(self):
        d=sample();d.update(initial=1,requests=[-1],discharge_efficiency={'numerator':2,'denominator':3});r=analyze(d);self.assertEqual(r['grid_out'],'2/3');self.assertEqual(r['loss'],'1/3');self.assertEqual(r['unmet'],'1/3')
    def test_empty(self):
        d=sample();d['requests']=[];self.assertEqual(analyze(d)['final'],'20')
    def test_invalid_efficiency(self):
        d=sample();d['charge_efficiency']={'numerator':2,'denominator':1};self.bad(d)
    def test_limits(self):
        d=sample();d['max_charge']=0;d['requests']=[20];self.assertEqual(analyze(d)['final'],'20')
    def test_random_balance_and_bounds(self):
        rng=random.Random(2)
        for _ in range(30):
            d=sample();d['requests']=[rng.randrange(-200,201) for _ in range(30)];r=analyze(d);self.assertEqual(r['conservation_residual'],'0')
            for s in r['steps']:
                self.assertTrue(0<=Fraction(s['after'])<=100);self.assertGreaterEqual(Fraction(s['loss']),0);self.assertEqual(Fraction(s['before'])+Fraction(s['grid_in'])-Fraction(s['grid_out'])-Fraction(s['loss']),Fraction(s['after']))

class Command(unittest.TestCase):
    def call(self,source,output):
        return subprocess.run([sys.executable,'-m','storage_dispatch.cli','--source',str(source),'--output',str(output)],capture_output=True,text=True)
    def test_cli_replay(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'out.json';src=ROOT/'fixtures/input.json';r=self.call(src,out);self.assertEqual(r.returncode,0,r.stderr);first=out.read_bytes();self.assertEqual(json.loads(first)['source_sha256'],hashlib.sha256(src.read_bytes()).hexdigest());self.assertEqual(self.call(src,out).returncode,0);self.assertEqual(first,out.read_bytes())
    def test_failure_preserves(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'bad.json';out=Path(td)/'out.json';out.write_text('prior')
            for body in ['{}','{"a":1,"a":2}','NaN']:
                src.write_text(body);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(out.read_text(),'prior')
    def test_same_path_and_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'in.json';src.write_bytes((ROOT/'fixtures/input.json').read_bytes());before=src.read_bytes();self.assertEqual(self.call(src,src).returncode,2);self.assertEqual(src.read_bytes(),before);out=Path(td)/'link.json';out.symlink_to(src);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(src.read_bytes(),before)
