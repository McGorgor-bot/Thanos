import sys,types
st=types.SimpleNamespace(session_state={}, cache_data=lambda **kw: (lambda f: f), cache_resource=lambda **kw: (lambda f: f))
sys.modules['streamlit']=st
from thanos.cad import parse_dxf_blocks
from collections import Counter
p='/mnt/data/GRO-PP-ELE-Rzut SSP.dxf'; data=open(p,'rb').read()
df=parse_dxf_blocks(data, True, count_mode='Wg bloków CAD')
print('rows',len(df))
print(Counter(df['element_name']).most_common(20))
print(df[df['kind']=='META'].to_dict('records'))
