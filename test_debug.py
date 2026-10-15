import sys,types,importlib.util
st=types.SimpleNamespace(session_state={}, cache_data=lambda **kw:(lambda f:f), cache_resource=lambda **kw:(lambda f:f));sys.modules['streamlit']=st
import types as T
pkg=T.ModuleType('thanos'); pkg.__path__=['/mnt/data/work/thanos'];sys.modules['thanos']=pkg
spec=importlib.util.spec_from_file_location('thanos.cad_debug','/mnt/data/work/thanos/cad_debug.py'); m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)
d=open('/mnt/data/GRO-PP-ELE-Rzut SSP.dxf','rb').read();m.parse_dxf_blocks(d,True,count_mode='Wg bloków CAD')
