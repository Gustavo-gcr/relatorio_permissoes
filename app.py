# -*- coding: utf-8 -*-
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


@st.cache_data(ttl=600, show_spinner=False)
def fetch_empresas_obras():
    """Lista de empresas/obras cadastradas, para seleção dinâmica
    (evita digitar código manualmente). Ajuste os nomes de
    tabela/coluna caso sejam diferentes no seu banco."""
    sql = """
        SELECT
            Empresas.Codigo_emp AS empresa,
            Empresas.Desc_emp AS nome_empresa,
            Obras.Cod_obr AS obra,
            Obras.Descr_obr AS nome_obra,
            Obras.UF_obr AS uf
        FROM Obras
        INNER JOIN Empresas ON Empresas.Codigo_emp = Obras.Empresa_obr
        ORDER BY Empresas.Codigo_emp, Obras.Cod_obr
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
    -- fn_ListEmpObr recebe só códigos de EMPRESA separados por vírgula
    -- (ex: "1,2") e devolve todas as obras dessas empresas.
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
    obra_pares: list[tuple[str, str]] | None = None,
):
    """Monta o SQL final com parâmetros (bind) de acordo com os filtros."""
    sql = BASE_SELECT
    params: list = [empresa_obra]

    if obra_pares:
        # restringe a obras específicas (dentro das empresas já filtradas
        # via fn_ListEmpObr); cada par é (empresa, obra)
        cond = " OR ".join(["(BDperm.empresa = ? AND BDperm.obra = ?)"] * len(obra_pares))
        sql += f" AND ({cond})"
        for emp, obr in obra_pares:
            params.extend([emp, obr])

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

    # ---- Empresa / Obra (seleção dinâmica, sem digitar código) ----
    st.subheader("Empresa / Obra")
    try:
        df_emp_obr = fetch_empresas_obras()
    except Exception as e:
        df_emp_obr = pd.DataFrame(columns=["empresa", "nome_empresa", "obra", "nome_obra", "uf"])
        st.warning(f"Não foi possível carregar empresas/obras: {e}")

    opcoes_empresa = [
        f"{e} - {n}" for e, n in
        df_emp_obr[["empresa", "nome_empresa"]].drop_duplicates().itertuples(index=False)
    ]
    todas_empresas = st.checkbox("Todas as empresas", value=True)
    empresas_escolhidas = (
        opcoes_empresa if todas_empresas
        else st.multiselect("Empresa", opcoes_empresa)
    )
    empresas_cod = [o.split(" - ")[0] for o in empresas_escolhidas]
    if not empresas_cod:
        empresas_cod = df_emp_obr["empresa"].astype(str).unique().tolist()
    # fn_ListEmpObr recebe só os códigos de empresa (formato original)
    empresa_obra = ",".join(empresas_cod) if empresas_cod else "1"

    df_obras_filtro = df_emp_obr[df_emp_obr["empresa"].astype(str).isin(empresas_cod)]
    opcoes_obra = [
        f"{r.empresa}-{r.obra} - {r.nome_obra}" for r in df_obras_filtro.itertuples()
    ]
    todas_obras = st.checkbox("Todas as obras", value=True)
    obra_pares: list[tuple[str, str]] = []
    if not todas_obras:
        obras_escolhidas = st.multiselect("Obra (digite para filtrar)", opcoes_obra)
        for o in obras_escolhidas:
            emp, obr = o.split(" - ")[0].split("-", 1)
            obra_pares.append((emp, obr))
    # se "todas as obras" -> obra_pares fica vazio -> não restringe obra,
    # pega todas as obras das empresas selecionadas

    st.divider()

    # ---- Programa / Status / Permissão ----
    c1, c2 = st.columns(2)
    programa = c1.text_input("Programa", value="%")
    permissao = c2.text_input("Permissão", value="%")
    status_opcao = st.selectbox("Status do usuário", ["Todos", "Ativo", "Inativo"])
    status_usr = {"Todos": "%", "Ativo": "A", "Inativo": "I"}[status_opcao]

    st.divider()

    # ---- Usuário ou Grupo ----
    st.subheader("Usuários")
    modo = st.radio("Filtrar por", ["Usuário", "Código de Grupo"], horizontal=True)

    usuarios_sel: list[str] = []
    grupos_sel: list[str] = []

    if modo == "Usuário":
        if not st.checkbox("Todos os usuários", value=True):
            try:
                opcoes = [f"{r.login} - {r.nome}" for r in fetch_usuarios().itertuples()]
            except Exception as e:
                opcoes = []
                st.warning(f"Erro ao carregar usuários: {e}")
            escolhidos = st.multiselect("Usuário (digite para filtrar)", opcoes)
            manual = st.text_input("Ou logins manuais (vírgula)")
            usuarios_sel = list(dict.fromkeys(
                [o.split(" - ")[0].strip() for o in escolhidos]
                + [u.strip() for u in manual.split(",") if u.strip()]
            ))
    else:
        try:
            opcoes_grupo = fetch_grupos()["grupo"].astype(str).tolist()
        except Exception as e:
            opcoes_grupo = []
            st.warning(f"Erro ao carregar grupos: {e}")
        grupos_sel = st.multiselect("Grupo (digite para filtrar)", opcoes_grupo)
        manual_grupo = st.text_input("Ou grupos manuais (vírgula)")
        grupos_sel = list(dict.fromkeys(
            grupos_sel + [g.strip() for g in manual_grupo.split(",") if g.strip()]
        ))

    st.divider()
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
            obra_pares=obra_pares,
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