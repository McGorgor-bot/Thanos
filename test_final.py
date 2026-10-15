import sys,types
st=types.SimpleNamespace(session_state={}, cache_data=lambda **kw:(lambda f:f), cache_resource=lambda **kw:(lambda f:f))
sys.modules['streamlit']=st
from thanos.cad import parse_dxf_blocks,_legend_display_name_map
from thanos.export import apply_legend_names_to_results
from collections import Counter
p='/mnt/data/GRO-PP-ELE-Rzut SSP.dxf';d=open(p,'rb').read()
df=parse_dxf_blocks(d,True,count_mode='Wg bloków CAD')
rows=df.to_dict('records')
mp=_legend_display_name_map(d,True)
res=apply_legend_names_to_results(rows, {'GRO-PP-ELE-Rzut SSP.dxf':mp})
c=Counter(r.get('Nazwa z legendy') or r.get('Nazwa bloku') or r.get('element_name') for r in res if r.get('kind')!='META')
for k,v in c.most_common(20): print(k,v)
print('meta',[r for r in res if r.get('kind')=='META'])


def test_ath_recovers_invalid_rms_mapping_from_element_name():
    from thanos.ath import load_excel, parse_ath_template, suggest_mapping, generate_ath
    from pathlib import Path
    xlsx = Path('/mnt/data/Zestawienie elementów_2026-10-08_11-01-58.xlsx')
    ath = Path('/mnt/data/20260527_Teatr_Rzeszów_IE_IT.ath')
    if not xlsx.exists() or not ath.exists():
        return
    p = parse_ath_template(ath.read_bytes())
    m = suggest_mapping(load_excel(xlsx.read_bytes()), p, 'Nazwa bloku')
    # Simulate the exact failure seen in the UI: RMS ZEST contains element names.
    m['RMS ZEST'] = m['Element']
    out = generate_ath(ath.read_bytes(), m, 'Regression ATH')
    text = out.decode('cp1250')
    assert 'KNNR\t5 0502-03\t5\t0502-03' in text
    assert '[POZYCJA]' in text
