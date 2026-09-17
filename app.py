import io
import datetime as dt

import pandas as pd
import streamlit as st
import pyodbc

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
)

# --------------------------------------------------------------------------
# CONFIG DA PÁGINA
# --------------------------------------------------------------------------
st.set_page_config(page_title="Permissões de Usuários - UAU", layout="wide")


# --------------------------------------------------------------------------
# CONEXÃO COM O BANCO (credenciais via st.secrets)
# --------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def get_connection():
    cfg = st.secrets["uau"]
    conn_str = (
        "DRIVER={ODBC Driver 17 for SQL Server};"
        f"SERVER={cfg['server']};"
        f"DATABASE={cfg['database']};"
        f"UID={cfg['uid']};"
        f"PWD={cfg['pwd']};"
        "TrustServerCertificate=yes;"
    )
    return pyodbc.connect(conn_str, timeout=15)


def run_query(sql: str, params: list | None = None) -> pd.DataFrame:
    conn = get_connection()
    return pd.read_sql(sql, conn, params=params or [])


# --------------------------------------------------------------------------
# LISTAS AUXILIARES (usuários e grupos cadastrados)
# --------------------------------------------------------------------------
@st.cache_data(ttl=600, show_spinner=False)
def fetch_usuarios():
    sql = """
        SELECT Login_usr AS login, Nome_usr AS nome
        FROM Usuarios
        ORDER BY Nome_usr
    """
    return run_query(sql)


@st.cache_data(ttl=600, show_spinner=False)
def fetch_grupos():
    sql = """
        SELECT DISTINCT Grupo_usr AS grupo
        FROM Usuarios
        WHERE Grupo_usr IS NOT NULL AND Grupo_usr <> ''
        ORDER BY Grupo_usr
    """
    return run_query(sql)


# --------------------------------------------------------------------------
# MONTAGEM DA QUERY PRINCIPAL (a mesma lógica do SQL enviado, agora
# com bind de parâmetros e com o filtro de Grupo_usr adicionado)
# --------------------------------------------------------------------------
BASE_SELECT = """
SELECT *,
    CASE WHEN PermissaOBra = 0 THEN
        (CASE WHEN PermIndivi = 0 THEN PermiGrupo ELSE PermIndivi END)
    ELSE PermissaOBra
    END AS teste
FROM (
    SELECT
        Login_usr AS usuario,
        Nome_usr AS nome,
        ObrUsr.Emp_uo AS empresa,
        ObrUsr.Obr_uo AS obra,
        Status_usr AS usuarioStatus,
        Grupo_usr AS GrupoUsuario,
        Programas.Cod_prg AS codPr,
        Programas.Tit_prg AS Progdes1,
        Programas.Obs_prg AS Progdes2,
        COALESCE((
            SELECT Nivel_po
            FROM ObrUsrPerm
            WHERE ObrUsrPerm.Emp_po = ObrUsr.Emp_uo
              AND ObrUsrPerm.Obra_po = ObrUsr.Obr_uo
              AND ObrUsrPerm.Prg_po = Programas.Cod_prg
              AND ObrUsrPerm.Usr_po = Login_usr
        ), '') AS PermissaOBra,
        COALESCE((
            SELECT Atrib_up
            FROM UsrProg
            WHERE UsrProg.User_up = Login_usr
              AND UsrProg.Prog_up = Programas.Cod_prg
        ), 0) AS PermIndivi,
        COALESCE((
            SELECT GrpProg.Atrib_gp
            FROM GrpProg
            WHERE GrpProg.Grp_gp = Usuarios.Grupo_usr
              AND GrpProg.Prog_gp = Programas.Cod_prg
        ), 0) AS PermiGrupo
    FROM Usuarios
    LEFT JOIN ObrUsr ON ObrUsr.Usr_uo = Usuarios.Login_usr
    LEFT JOIN Programas ON Programas.Status_prg = 0
) BDperm
INNER JOIN (
    SELECT * FROM fn_ListEmpObr(?, ',')
) AS EmpObr
    ON BDperm.empresa = EmpObr.Empresa
   AND BDperm.obra = EmpObr.Obra
WHERE 1 = 1
"""

ORDER_BY = " ORDER BY usuario, BDperm.empresa, BDperm.obra"


def build_query(
    empresa_obra: str,
    modo: str,
    usuarios_sel: list[str],
    grupos_sel: list[str],
    programa: str,
    status_usr: str,
    permissao: str,
):
    """Monta o SQL final com parâmetros (bind) de acordo com os filtros."""
    sql = BASE_SELECT
    params: list = [empresa_obra]

    if modo == "Usuário" and usuarios_sel:
        placeholders = ",".join(["?"] * len(usuarios_sel))
        sql += f" AND BDperm.usuario IN ({placeholders})"
        params.extend(usuarios_sel)
    # se modo == "Usuário" e a lista estiver vazia (ex.: "Selecionar todos"),
    # não filtra por usuário -> pega todos.

    if modo == "Código de Grupo" and grupos_sel:
        placeholders = ",".join(["?"] * len(grupos_sel))
        sql += f" AND BDperm.GrupoUsuario IN ({placeholders})"
        params.extend(grupos_sel)

    sql += " AND codPr LIKE ?"
    params.append(programa)

    sql += " AND usuarioStatus LIKE ?"
    params.append(status_usr)

    sql += """ AND (
        CASE
            WHEN PermissaOBra = 0 THEN
                CASE WHEN PermIndivi = 0 THEN PermiGrupo ELSE PermIndivi END
            ELSE PermissaOBra
        END
    ) LIKE ?"""
    params.append(permissao)

    sql += ORDER_BY
    return sql, params


# --------------------------------------------------------------------------
# UI - FILTROS
# --------------------------------------------------------------------------
st.title("Relatório de Permissões de Usuários")

with st.sidebar:
    st.header("Filtros")

    empresa_obra = st.text_input(
        "Empresa/Obra (fn_ListEmpObr)",
        value="1",
        help="Lista de empresas/obras separadas por vírgula, ex: 1,2,3",
    )

    programa = st.text_input("Programa (codPr)", value="%")
    permissao = st.text_input("Permissão (nível)", value="%")

    status_opcao = st.selectbox(
        "Status do usuário", ["Todos", "Ativo", "Inativo"], index=0
    )
    status_map = {"Todos": "%", "Ativo": "A", "Inativo": "I"}
    status_usr = status_map[status_opcao]

    st.markdown("---")
    st.subheader("Filtrar por")

    modo = st.radio(
        "Tipo de filtro",
        ["Usuário", "Código de Grupo"],
        horizontal=False,
    )

    usuarios_sel: list[str] = []
    grupos_sel: list[str] = []

    if modo == "Usuário":
        selecionar_todos_usr = st.checkbox("Selecionar todos os usuários", value=True)

        if not selecionar_todos_usr:
            try:
                df_usuarios = fetch_usuarios()
                opcoes = [f"{r.login} - {r.nome}" for r in df_usuarios.itertuples()]
            except Exception as e:
                opcoes = []
                st.warning(f"Não foi possível carregar a lista de usuários: {e}")

            escolhidos = st.multiselect("Selecionar usuários cadastrados", opcoes)
            escolhidos_login = [o.split(" - ")[0].strip() for o in escolhidos]

            manual = st.text_area(
                "Ou digite os logins separados por vírgula",
                placeholder="ex: adriana, alecarla, joao.silva",
            )
            manual_login = [u.strip() for u in manual.split(",") if u.strip()]

            usuarios_sel = list(dict.fromkeys(escolhidos_login + manual_login))
        # se "selecionar todos" -> usuarios_sel fica vazio -> query não filtra por usuário

    else:  # Código de Grupo
        st.caption("Ao filtrar por grupo, todos os usuários são considerados automaticamente.")
        modo_grupo = st.radio(
            "Como informar o(s) grupo(s)",
            ["Escolher da lista cadastrada", "Digitar manualmente"],
            horizontal=False,
        )

        if modo_grupo == "Escolher da lista cadastrada":
            try:
                df_grupos = fetch_grupos()
                opcoes_grupo = df_grupos["grupo"].astype(str).tolist()
            except Exception as e:
                opcoes_grupo = []
                st.warning(f"Não foi possível carregar a lista de grupos: {e}")
            grupos_sel = st.multiselect("Selecionar grupo(s) (Grupo_usr)", opcoes_grupo)
        else:
            manual_grupo = st.text_input(
                "Digite o(s) código(s) de grupo separados por vírgula",
                placeholder="ex: 008, 043, 042",
            )
            grupos_sel = [g.strip() for g in manual_grupo.split(",") if g.strip()]

    st.markdown("---")
    gerar = st.button("Gerar relatório", type="primary", use_container_width=True)


# --------------------------------------------------------------------------
# EXECUÇÃO DA CONSULTA
# --------------------------------------------------------------------------
if gerar:
    if modo == "Código de Grupo" and not grupos_sel:
        st.error("Selecione ou digite ao menos um código de grupo.")
    else:
        sql, params = build_query(
            empresa_obra=empresa_obra,
            modo=modo,
            usuarios_sel=usuarios_sel,
            grupos_sel=grupos_sel,
            programa=programa,
            status_usr=status_usr,
            permissao=permissao,
        )
        try:
            with st.spinner("Consultando..."):
                df = run_query(sql, params)
            st.session_state["df_resultado"] = df
            st.session_state["pdf_bytes"] = None  # invalida PDF anterior
        except Exception as e:
            st.error(f"Erro ao consultar o banco: {e}")


# --------------------------------------------------------------------------
# EXIBIÇÃO DO RESULTADO (agrupado por usuário, igual ao layout original)
# --------------------------------------------------------------------------
COLS_EXIBICAO = ["codPr", "Progdes1", "Progdes2", "empresa", "obra", "teste"]
COLS_LABEL = ["Programa", "Descrição", "Obs", "Emp", "Obra", "Atributo"]

if "df_resultado" in st.session_state:
    df = st.session_state["df_resultado"]

    if df.empty:
        st.info("Nenhum registro encontrado para os filtros selecionados.")
    else:
        st.success(f"{len(df)} registros encontrados.")

        for (login, nome), grupo_df in df.groupby(["usuario", "nome"], sort=True):
            st.markdown(f"**{login}  -  {nome}**")
            tabela = grupo_df[COLS_EXIBICAO].copy()
            tabela.columns = COLS_LABEL
            st.dataframe(tabela, use_container_width=True, hide_index=True)

        st.markdown("---")
        gerar_pdf = st.button("Gerar PDF do relatório")

        if gerar_pdf:
            with st.spinner("Gerando PDF..."):

                def montar_pdf(df: pd.DataFrame) -> bytes:
                    buffer = io.BytesIO()
                    doc = SimpleDocTemplate(
                        buffer,
                        pagesize=A4,
                        topMargin=1.5 * cm,
                        bottomMargin=1.5 * cm,
                        leftMargin=1.2 * cm,
                        rightMargin=1.2 * cm,
                    )
                    styles = getSampleStyleSheet()
                    titulo_style = ParagraphStyle(
                        "Titulo", parent=styles["Title"], fontSize=16, alignment=1
                    )
                    usuario_style = ParagraphStyle(
                        "Usuario", parent=styles["Heading3"], fontSize=11,
                        spaceBefore=10, spaceAfter=4,
                    )
                    cell_style = ParagraphStyle(
                        "Cell", parent=styles["Normal"], fontSize=7.5, leading=9
                    )
                    header_style = ParagraphStyle(
                        "Header", parent=styles["Normal"], fontSize=8,
                        leading=10, textColor=colors.white,
                    )

                    elementos = []

                    agora = dt.datetime.now()
                    cabecalho_tbl = Table(
                        [
                            ["LCM CONSTRUÇÃO", "Título", agora.strftime("%A, %d de %B de %Y")],
                            ["", "", f"Usuário: GREIS"],
                            ["", "", f"Horário: {agora.strftime('%H:%M:%S')}"],
                        ],
                        colWidths=[6 * cm, 8 * cm, 6 * cm],
                    )
                    cabecalho_tbl.setStyle(TableStyle([
                        ("FONTSIZE", (0, 0), (-1, -1), 8),
                        ("ALIGN", (2, 0), (2, -1), "RIGHT"),
                        ("ALIGN", (1, 0), (1, 0), "CENTER"),
                        ("FONTSIZE", (1, 0), (1, 0), 16),
                        ("FONTNAME", (1, 0), (1, 0), "Helvetica-Bold"),
                        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ]))
                    elementos.append(cabecalho_tbl)
                    elementos.append(Spacer(1, 0.6 * cm))

                    header_row = [Paragraph(h, header_style) for h in
                                  ["Programa", "Descrição", "Obs", "Emp/Obra", "Atrib"]]

                    for (login, nome), grupo_df in df.groupby(["usuario", "nome"], sort=True):
                        elementos.append(Paragraph(f"{login}  -  {nome}", usuario_style))

                        linhas = [header_row]
                        for r in grupo_df.itertuples():
                            linhas.append([
                                Paragraph(str(r.codPr), cell_style),
                                Paragraph(str(r.Progdes1), cell_style),
                                Paragraph(str(r.Progdes2), cell_style),
                                Paragraph(f"{r.empresa} / {r.obra}", cell_style),
                                Paragraph(str(r.teste), cell_style),
                            ])

                        tbl = Table(
                            linhas,
                            colWidths=[2 * cm, 3.2 * cm, 8 * cm, 2.8 * cm, 1.5 * cm],
                            repeatRows=1,
                        )
                        tbl.setStyle(TableStyle([
                            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4472C4")),
                            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F2F2")]),
                        ]))
                        elementos.append(tbl)
                        elementos.append(Spacer(1, 0.4 * cm))

                    doc.build(elementos)
                    buffer.seek(0)
                    return buffer.read()

                st.session_state["pdf_bytes"] = montar_pdf(df)

        if st.session_state.get("pdf_bytes"):
            st.download_button(
                label="Baixar PDF",
                data=st.session_state["pdf_bytes"],
                file_name=f"relatorio_permissoes_{dt.date.today().isoformat()}.pdf",
                mime="application/pdf",
            )
else:
    st.info("Configure os filtros na barra lateral e clique em **Gerar relatório**.")