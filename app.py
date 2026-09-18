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
def _abrir_conexao():
    cfg = st.secrets["uau"]
    conn_str = (
        "DRIVER={ODBC Driver 17 for SQL Server};"
        f"SERVER={cfg['server']};"
        f"DATABASE={cfg['database']};"
        f"UID={cfg['uid']};"
        f"PWD={cfg['pwd']};"
        "TrustServerCertificate=yes;"
    )
    conn = pyodbc.connect(conn_str, timeout=15)
    # timeout de EXECUÇÃO da query (em segundos), separado do timeout de
    # login acima.
    conn.timeout = 300
    return conn


def _conexao_esta_quebrada(exc: Exception) -> bool:
    """Detecta erros de conexão (rede/driver), diferentes de erros de SQL
    (sintaxe, permissão, dados). Nesses casos vale a pena reconectar."""
    sqlstate = getattr(exc, "args", [None])[0] if getattr(exc, "args", None) else None
    return sqlstate in ("08S01", "08001", "08003", "08004", "HYT00", "HYT01")


def run_query(sql: str, params: list | None = None) -> pd.DataFrame:
    """Abre uma conexão NOVA para cada consulta e fecha em seguida.

    Antes o app usava uma única conexão global (@st.cache_resource) durante
    toda a vida do processo. Isso funciona bem em rede estável, mas basta a
    conexão cair uma vez (VPN, firewall, instabilidade, servidor reiniciando
    — erro 08S01 "Communication link failure"/"connection reset") para que
    TODAS as consultas seguintes falhem, mesmo as mais simples, até alguém
    reiniciar o app manualmente.

    Abrir/fechar uma conexão por consulta custa um pouco mais de tempo,
    mas evita esse tipo de trava total, já que nunca fica uma conexão
    "zumbi" presa em cache. Ainda assim, se a rede estiver instável na
    hora exata da chamada, tentamos reconectar uma vez antes de desistir.
    """
    try:
        with _abrir_conexao() as conn:
            return pd.read_sql(sql, conn, params=params or [])
    except pyodbc.Error as e:
        if _conexao_esta_quebrada(e):
            with _abrir_conexao() as conn:
                return pd.read_sql(sql, conn, params=params or [])
        raise


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
def fetch_programas():
    """Lista de programas (Prg_po) distintos cadastrados em ObrUsrPerm,
    para alimentar o multiselect de "Programa" na barra lateral."""
    sql = """
        SELECT DISTINCT Prg_po
        FROM ObrUsrPerm
        ORDER BY Prg_po
    """
    return run_query(sql)


@st.cache_data(ttl=600, show_spinner=False)
def fetch_niveis_permissao():
    """Lista de níveis (Nivel_po) distintos cadastrados em ObrUsrPerm,
    para alimentar o multiselect de "Permissão" na barra lateral."""
    sql = """
        SELECT DISTINCT Nivel_po
        FROM ObrUsrPerm
        ORDER BY Nivel_po
    """
    return run_query(sql)


@st.cache_data(ttl=600, show_spinner=False)
def fetch_empresas_obras():
    """Lista de empresas/obras cadastradas, para seleção dinâmica
    (evita digitar código manualmente). Traz direto de Empresas/Obras
    (sem passar por fn_ListEmpObr) — o multiselect de "Obra" na barra
    lateral já filtra client-side (via pandas) apenas as obras das
    empresas selecionadas."""
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


def normaliza_codigos(codigos: list[str]) -> list[str]:
    """Remove vazios/'nan'/espaços de uma lista de códigos, mantendo a
    ordem e sem duplicar."""
    limpos = [str(c).strip() for c in codigos if c is not None]
    limpos = [c for c in limpos if c and c.lower() != "nan"]
    return list(dict.fromkeys(limpos))


# --------------------------------------------------------------------------
# MONTAGEM DA QUERY PRINCIPAL
# --------------------------------------------------------------------------
# fn_ListEmpObr foi REMOVIDA da consulta. O filtro de empresa/obra agora é
# feito direto no WHERE, usando os códigos que o usuário escolhe na barra
# lateral (carregados direto de Empresas/Obras). Isso resolve de vez o
# erro 537: o parâmetro @tcList dessa função é do tipo T-SQL `text`, e o
# driver ODBC 17 trunca parâmetros bindados nesse tipo — o problema não
# existe mais porque a função deixou de ser chamada.
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
WHERE 1 = 1
"""

ORDER_BY = " ORDER BY usuario, BDperm.empresa, BDperm.obra"


def build_query(
    empresas_cod: list[str],
    modo: str,
    usuarios_sel: list[str],
    grupos_sel: list[str],
    programa_sel: list[str],
    status_usr: str,
    permissao_sel: list[str],
    obra_pares: list[tuple[str, str]] | None = None,
):
    """Monta o SQL final com parâmetros (bind) de acordo com os filtros.

    Filtro de empresa/obra:
    - Se `obra_pares` vier preenchido (o usuário desmarcou "Todas as obras"
      e escolheu obras específicas), filtra por cada par (empresa, obra).
    - Caso contrário, filtra só pelas empresas selecionadas
      (`BDperm.empresa IN (...)`), trazendo todas as obras delas — sem
      nenhuma chamada a fn_ListEmpObr.
    """
    sql = BASE_SELECT
    params: list = []

    if obra_pares:
        cond = " OR ".join(["(BDperm.empresa = ? AND BDperm.obra = ?)"] * len(obra_pares))
        sql += f" AND ({cond})"
        for emp, obr in obra_pares:
            params.extend([emp, obr])
    elif empresas_cod:
        placeholders = ",".join(["?"] * len(empresas_cod))
        sql += f" AND BDperm.empresa IN ({placeholders})"
        params.extend(empresas_cod)
    # se nem obra_pares nem empresas_cod vierem preenchidos, não filtra por
    # empresa/obra (pega tudo) — cenário improvável na prática, já que o
    # combo de empresas sempre tem ao menos um valor default.

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

    if programa_sel:
        placeholders = ",".join(["?"] * len(programa_sel))
        sql += f" AND codPr IN ({placeholders})"
        params.extend(programa_sel)
    # se programa_sel vier vazio (nada selecionado no multiselect), não
    # filtra por programa -> pega todos.

    sql += " AND usuarioStatus LIKE ?"
    params.append(status_usr)

    if permissao_sel:
        placeholders = ",".join(["?"] * len(permissao_sel))
        sql += f""" AND (
        CASE
            WHEN PermissaOBra = 0 THEN
                CASE WHEN PermIndivi = 0 THEN PermiGrupo ELSE PermIndivi END
            ELSE PermissaOBra
        END
    ) IN ({placeholders})"""
        params.extend(permissao_sel)
    # se permissao_sel vier vazio, não filtra por permissão -> pega todas.

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
    empresas_cod = normaliza_codigos([o.split(" - ")[0] for o in empresas_escolhidas])
    if not empresas_cod:
        empresas_cod = normaliza_codigos(df_emp_obr["empresa"].astype(str).unique().tolist())

    # Obras: filtradas dinamicamente (client-side, via pandas) para conter
    # apenas as obras pertencentes às empresas selecionadas acima —
    # sempre que a seleção de empresa muda, essa lista é recalculada.
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
    # pega todas as obras das empresas selecionadas (via empresas_cod)

    st.divider()

    # ---- Programa / Status / Permissão ----
    try:
        opcoes_programa = fetch_programas()["Prg_po"].astype(str).tolist()
    except Exception as e:
        opcoes_programa = []
        st.warning(f"Erro ao carregar programas: {e}")

    try:
        opcoes_permissao = fetch_niveis_permissao()["Nivel_po"].astype(str).tolist()
    except Exception as e:
        opcoes_permissao = []
        st.warning(f"Erro ao carregar níveis de permissão: {e}")

    # Multiselect dinâmico: nada selecionado = não filtra (traz todos),
    # igual ao comportamento antigo do "%".
    programa_sel = st.multiselect("Programa (vazio = todos)", opcoes_programa)
    permissao_sel = st.multiselect("Permissão (vazio = todas)", opcoes_permissao)

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
        try:
            sql, params = build_query(
                empresas_cod=empresas_cod,
                modo=modo,
                usuarios_sel=usuarios_sel,
                grupos_sel=grupos_sel,
                programa_sel=programa_sel,
                status_usr=status_usr,
                permissao_sel=permissao_sel,
                obra_pares=obra_pares,
            )
            with st.spinner("Consultando..."):
                df = run_query(sql, params)
            st.session_state["df_resultado"] = df
            st.session_state["pdf_bytes"] = None  # invalida PDF anterior
        except Exception as e:
            st.error(f"Erro ao consultar o banco: {e}")
            # Painel de debug: mostra o SQL final e os parâmetros enviados,
            # para facilitar identificar qual valor está quebrando a query
            with st.expander("Detalhes técnicos (debug)"):
                st.write("**empresas_cod selecionadas:**", empresas_cod)
                st.write("**obra_pares selecionados:**", obra_pares)
                try:
                    st.code(sql, language="sql")
                    st.write("**Parâmetros bindados (na ordem):**")
                    st.write(params)
                except NameError:
                    st.write("A query não chegou a ser montada.")


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