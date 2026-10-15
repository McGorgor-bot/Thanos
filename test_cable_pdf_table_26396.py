from thanos.cable_list import extract_cable_candidates

class Upload:
    def __init__(self, name, path): self.name, self.path = name, path
    def getvalue(self):
        with open(self.path, 'rb') as f: return f.read()

def test_e11_pdf_extracts_circuit_cable_and_destination():
    df = extract_cable_candidates([Upload('E11.pdf', '/mnt/data/E11.pdf')])
    assert not df.empty
    row = df[df['Oznaczenie obwodu'] == 'RK/7.1'].iloc[0]
    assert row['Typ kabla'].startswith('N2XH-J 5x6')
    assert row['Odbiornik / rozdzielnica docelowa'] == 'Piec konwekcyjny'
    assert row['Rozdzielnica źródłowa'] == 'RK'

def test_e11_pdf_keeps_multiple_circuits():
    df = extract_cable_candidates([Upload('E11.pdf', '/mnt/data/E11.pdf')])
    assert {'RK/3.1', 'RK/7.1', 'RK/15.3'}.issubset(set(df['Oznaczenie obwodu']))
