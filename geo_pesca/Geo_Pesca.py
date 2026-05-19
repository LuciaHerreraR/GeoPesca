# -*- coding: utf-8 -*-
"""
GeoPesca - Plugin de QGIS para reconstrucción de trayectorias de pesca
y detección de zonas de actividad pesquera a partir de datos GPS.

Flujo principal:
  1. Carga puntos GPS desde CSV o GeoPackage.
  2. Agrupa los puntos por barco y jornada (barco + fecha).
  3. Reconstruye la trayectoria de cada jornada como línea.
  4. Genera segmentos entre puntos consecutivos y calcula velocidad,
     distancia, tiempo y ángulo de giro.
  5. Detecta tandas de pesca según umbrales configurables por arte.
  6. Exporta los resultados a un GeoPackage y los carga en QGIS.
"""

import math
import os
import json

from osgeo import ogr

from qgis.PyQt.QtCore import (
    QSettings, QTranslator, QCoreApplication, QVariant, QDateTime
)
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QAction, QFileDialog, QDialogButtonBox, QInputDialog
)
from qgis.core import (
    QgsVectorLayer,
    QgsProject,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsWkbTypes,
    QgsVectorFileWriter,
    QgsFeatureRequest,
    QgsExpression,
    QgsSpatialIndex,
)

from .resources import *
from .Geo_Pesca_dialog import geopescaDialog


class geopesca:
    """
    Clase principal del plugin GeoPesca.

    Gestiona la interfaz gráfica, la lectura de datos de entrada,
    el procesamiento de trayectorias y la exportación de resultados.
    """

    # ------------------------------------------------------------------
    # INICIALIZACIÓN
    # ------------------------------------------------------------------

    def __init__(self, iface):
        """
        Constructor del plugin.

        Parámetros
        ----------
        iface : QgisInterface
            Referencia a la interfaz de QGIS, necesaria para añadir
            botones, menús y acceder al mapa activo.
        """
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)

        # Carga el fichero de traducción según el idioma configurado en QGIS.
        locale = QSettings().value("locale/userLocale")[0:2]
        locale_path = os.path.join(
            self.plugin_dir, "i18n", f"geopesca_{locale}.qm"
        )
        if os.path.exists(locale_path):
            self.translator = QTranslator()
            self.translator.load(locale_path)
            QCoreApplication.installTranslator(self.translator)

        self.actions = []
        self.menu = self.tr(u"&GeoPesca")
        self.first_start = None

        # Nombre de la subcapa seleccionada cuando la entrada es un GeoPackage.
        self.subcapa_gpkg = None

        # Diccionario con los umbrales por arte de pesca, cargados desde JSON.
        self.parametros_artes = {}
        self.cargar_parametros_artes()

    def tr(self, message):
        """Devuelve la traducción de *message* al idioma activo de QGIS."""
        return QCoreApplication.translate("geopesca", message)

    def log(self, texto):
        """
        Escribe una línea de texto en el panel de log del diálogo.

        Si el diálogo aún no existe (p. ej. durante la inicialización)
        el mensaje se descarta silenciosamente.
        """
        try:
            self.dlg.txtLog.appendPlainText(str(texto))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # REGISTRO DE ACCIONES EN LA INTERFAZ DE QGIS
    # ------------------------------------------------------------------

    def add_action(
        self,
        icon_path,
        text,
        callback,
        enabled_flag=True,
        add_to_menu=True,
        add_to_toolbar=True,
        status_tip=None,
        whats_this=None,
        parent=None,
    ):
        """
        Crea una QAction, la conecta al callback y la registra en la
        barra de herramientas y en el menú del plugin.

        Devuelve la acción creada.
        """
        icon = QIcon(icon_path)
        action = QAction(icon, text, parent)
        action.triggered.connect(callback)
        action.setEnabled(enabled_flag)

        if add_to_toolbar:
            self.iface.addToolBarIcon(action)
        if add_to_menu:
            self.iface.addPluginToMenu(self.menu, action)

        self.actions.append(action)
        return action

    def initGui(self):
        """Añade el icono y el ítem de menú al arrancar QGIS."""
        icon_path = os.path.join(self.plugin_dir, "icon.png")
        self.add_action(
            icon_path,
            text=self.tr(u"GeoPesca"),
            callback=self.run,
            parent=self.iface.mainWindow(),
        )
        self.first_start = True

    def unload(self):
        """Elimina el icono y el ítem de menú al desactivar el plugin."""
        for action in self.actions:
            self.iface.removePluginMenu(self.tr(u"&GeoPesca"), action)
            self.iface.removeToolBarIcon(action)

    # ------------------------------------------------------------------
    # CARGA Y APLICACIÓN DE PARÁMETROS POR ARTE DE PESCA
    # ------------------------------------------------------------------

    def cargar_parametros_artes(self):
        """
        Lee el fichero «parametros_artes_pesca.json» situado en el
        directorio del plugin y almacena su contenido en
        ``self.parametros_artes``.

        El JSON tiene la estructura:
        {
            "cerco": { "nudos": 3.5, "distancia": 200, ... },
            "palangre": { ... },
            ...
        }

        Si el fichero no existe o hay un error de lectura se deja el
        diccionario vacío y se registra el error en el log.
        """
        ruta_json = os.path.join(self.plugin_dir, "parametros_artes_pesca.json")

        if not os.path.exists(ruta_json):
            self.parametros_artes = {}
            return

        try:
            with open(ruta_json, "r", encoding="utf-8") as f:
                self.parametros_artes = json.load(f)
        except Exception as e:
            self.parametros_artes = {}
            self.log(f"No se pudo leer parametros_artes_pesca.json: {e}")

    def aplicar_parametros_arte(self, *args):
        """
        Rellena los campos numéricos del diálogo con los valores
        predefinidos para el arte de pesca actualmente seleccionado
        en el desplegable.

        Si el arte no está en el JSON se registra un aviso pero no
        se modifica ningún campo.
        """
        arte_original = self.dlg.cmbArtePesca.currentText()
        arte = self.normalizar_nombre(arte_original)

        if arte not in self.parametros_artes:
            self.log(f"No hay parámetros definidos para: {arte}")
            return

        p = self.parametros_artes[arte]

        # Velocidad mínima (opcional, no todos los diálogos la tienen).
        if hasattr(self.dlg, "spnVelMinNudos"):
            self.dlg.spnVelMinNudos.setValue(float(p.get("nudos_min", 0.0)))

        self.dlg.spnUmbralNudos.setValue(float(p.get("nudos", 0.0)))
        self.dlg.spnUmbralDistancia.setValue(float(p.get("distancia", 0.0)))
        self.dlg.spnUmbralMaxTanda.setValue(float(p.get("max_tanda", 0.0)))
        self.dlg.spnMinLineasTanda.setValue(int(p.get("min_lineas", 1)))
        self.dlg.spnUmbralAngulo.setValue(float(p.get("angulo", 360.0)))
        self.dlg.spnLongitudMaxTanda.setValue(float(p.get("long_total", 999_999_999.0)))

        self.log(f"Parámetros cargados desde JSON para: {arte}")

    # ------------------------------------------------------------------
    # SELECCIÓN DE ARCHIVOS DE ENTRADA Y SALIDA
    # ------------------------------------------------------------------

    def seleccionar_entrada(self):
        """
        Abre un diálogo de selección de fichero (CSV o GPKG).

        Tras la selección:
        - Guarda la ruta en el campo de texto del diálogo.
        - Si es un GeoPackage, muestra un selector de subcapa.
        - Carga los nombres de campos en los desplegables de barco y fecha.
        """
        ruta, _ = QFileDialog.getOpenFileName(
            self.dlg,
            "Seleccionar archivo de entrada",
            "",
            "Archivos soportados (*.csv *.gpkg)",
        )

        if not ruta:
            return

        self.dlg.txtRutaCsv.setText(ruta)
        self.log(f"Archivo seleccionado: {ruta}")

        extension = os.path.splitext(ruta)[1].lower()

        if extension == ".gpkg":
            self.subcapa_gpkg = self.seleccionar_subcapa_gpkg(ruta)
            if self.subcapa_gpkg is None:
                self.log("No se seleccionó ninguna subcapa del GeoPackage.")
                return
            self.log(f"Subcapa seleccionada: {self.subcapa_gpkg}")
        else:
            self.subcapa_gpkg = None

        self.cargar_campos_entrada(ruta)

    def seleccionar_salida(self):
        """
        Abre un diálogo para elegir la carpeta donde se guardará el
        GeoPackage de resultados.
        """
        ruta = QFileDialog.getExistingDirectory(
            self.dlg, "Seleccionar carpeta de salida"
        )
        if ruta:
            self.dlg.txtRutaSalida.setText(ruta)
            self.log(f"Carpeta de salida seleccionada: {ruta}")

    def listar_subcapas_gpkg_puntos(self, ruta_gpkg):
        """
        Devuelve una lista con los nombres de las capas de puntos (2D
        o 2.5D) contenidas en el GeoPackage indicado.

        Utiliza la API de OGR directamente para no depender de QGIS
        en el momento de la consulta.
        """
        ds = ogr.Open(ruta_gpkg)
        if ds is None:
            self.log("No se pudo abrir el GeoPackage.")
            return []

        # Tipos de geometría de punto reconocidos por OGR.
        TIPOS_PUNTO = {
            ogr.wkbPoint,
            ogr.wkbMultiPoint,
            ogr.wkbPoint25D,
            ogr.wkbMultiPoint25D,
        }

        subcapas = [
            ds.GetLayerByIndex(i).GetName()
            for i in range(ds.GetLayerCount())
            if ds.GetLayerByIndex(i) is not None
            and ds.GetLayerByIndex(i).GetGeomType() in TIPOS_PUNTO
        ]

        ds = None  # Cierra el dataset OGR.
        return subcapas

    def seleccionar_subcapa_gpkg(self, ruta_gpkg):
        """
        Gestiona la selección de subcapa dentro de un GeoPackage.

        - Si no hay subcapas de puntos, devuelve None.
        - Si sólo hay una, la devuelve directamente.
        - Si hay varias, muestra un desplegable al usuario.
        """
        subcapas = self.listar_subcapas_gpkg_puntos(ruta_gpkg)

        if not subcapas:
            self.log("No se encontraron subcapas de puntos en el GeoPackage.")
            return None

        if len(subcapas) == 1:
            return subcapas[0]

        subcapa, ok = QInputDialog.getItem(
            self.dlg,
            "Seleccionar subcapa",
            "Elige la capa de puntos del GeoPackage:",
            subcapas,
            0,
            False,
        )

        return subcapa if ok and subcapa else None

    def obtener_capa_entrada(self, ruta_entrada):
        """
        Carga y devuelve la capa vectorial de puntos de entrada.

        Soporta:
        - CSV con coordenadas en los campos «longitude» y «latitude»,
          separados por «;» y decimales con «,».
        - GeoPackage con una capa de puntos previamente seleccionada.

        Devuelve None si el formato no es válido, la capa no se puede
        cargar o no contiene geometría de puntos.
        """
        extension = os.path.splitext(ruta_entrada)[1].lower()

        if extension == ".csv":
            # URI de texto delimitado para QGIS con los parámetros del CSV.
            uri = (
                "file:///" + ruta_entrada
                + "?type=csv"
                "&delimiter=;"
                "&decimalPoint=,"
                "&xField=longitude"
                "&yField=latitude"
                "&crs=EPSG:4326"
            )
            capa = QgsVectorLayer(uri, "entrada_temporal", "delimitedtext")

        elif extension == ".gpkg":
            if not self.subcapa_gpkg:
                self.log("No hay subcapa seleccionada del GeoPackage.")
                return None
            uri = f"{ruta_entrada}|layername={self.subcapa_gpkg}"
            capa = QgsVectorLayer(uri, self.subcapa_gpkg, "ogr")

        else:
            self.log("Formato no soportado. Usa CSV o GPKG.")
            return None

        if capa is None or not capa.isValid():
            self.log("No se pudo cargar la capa de entrada.")
            return None

        if QgsWkbTypes.geometryType(capa.wkbType()) != QgsWkbTypes.PointGeometry:
            self.log("La capa de entrada no es de puntos.")
            return None

        return capa

    def cargar_campos_entrada(self, ruta_entrada):
        """
        Rellena los desplegables «campo barco» y «campo fecha» con los
        nombres de los campos de la capa de entrada.

        Aplica heurísticas para preseleccionar el campo más probable
        en cada desplegable buscando palabras clave en el nombre del campo.
        """
        capa = self.obtener_capa_entrada(ruta_entrada)
        if capa is None:
            return

        campos = [field.name() for field in capa.fields()]

        self.dlg.cmbCampoBarco.clear()
        self.dlg.cmbCampoFecha.clear()
        self.dlg.cmbCampoBarco.addItems(campos)
        self.dlg.cmbCampoFecha.addItems(campos)

        # Palabras clave para autodetectar el campo de barco y fecha.
        PALABRAS_BARCO = {"barco", "embarc", "cfr"}
        PALABRAS_FECHA = {"time", "fecha", "hora", "timstmp", "datetime"}

        for campo in campos:
            nombre = campo.lower()
            if any(p in nombre for p in PALABRAS_BARCO):
                self.dlg.cmbCampoBarco.setCurrentText(campo)
            if any(p in nombre for p in PALABRAS_FECHA):
                self.dlg.cmbCampoFecha.setCurrentText(campo)

        self.log("Archivo cargado correctamente.")
        self.log(f"Campos detectados: {', '.join(campos)}")

    # ------------------------------------------------------------------
    # UTILIDADES GENERALES
    # ------------------------------------------------------------------

    def valor_float_seguro(self, valor):
        """
        Convierte *valor* a float de forma segura.

        Maneja None, cadenas vacías, «NULL» y el texto «QVariant()»
        que puede aparecer al leer campos nulos desde QGIS.
        Devuelve 0.0 si la conversión falla.
        """
        if valor is None:
            return 0.0

        texto = str(valor).strip()

        if texto in ("", "NULL", "QVariant()"):
            return 0.0

        try:
            return float(valor)
        except (ValueError, TypeError):
            try:
                # Intenta reemplazar coma decimal por punto.
                return float(texto.replace(",", "."))
            except (ValueError, TypeError):
                return 0.0

    def convertir_a_qdatetime(self, valor):
        """
        Convierte *valor* a QDateTime probando múltiples formatos de
        fecha/hora habituales en datos AIS y VMS.

        Normaliza previamente el texto eliminando sufijos UTC y
        sustituyendo la «T» separadora ISO 8601 por un espacio.

        Devuelve None si la conversión no tiene éxito con ningún formato.
        """
        if valor is None:
            return None

        if isinstance(valor, QDateTime) and valor.isValid():
            return valor

        # Normalización del texto de entrada.
        texto = (
            str(valor)
            .strip()
            .replace(" (UTC)", "")
            .replace("(UTC)", "")
            .replace("T", " ")
            .replace("Z", "")
            .strip()
        )

        FORMATOS = [
            "dd/MM/yyyy H:mm",
            "dd/MM/yyyy HH:mm",
            "dd/MM/yyyy H:mm:ss",
            "dd/MM/yyyy HH:mm:ss",
            "yyyy-MM-dd H:mm",
            "yyyy-MM-dd HH:mm",
            "yyyy-MM-dd H:mm:ss",
            "yyyy-MM-dd HH:mm:ss",
            "yyyy/MM/dd H:mm",
            "yyyy/MM/dd HH:mm",
            "yyyy/MM/dd H:mm:ss",
            "yyyy/MM/dd HH:mm:ss",
            "yyyy-MM-dd HH:mm:ss.zzz",
            "yyyy/MM/dd HH:mm:ss.zzz",
            "dd/MM/yyyy HH:mm:ss.zzz",
        ]

        for fmt in FORMATOS:
            dt = QDateTime.fromString(texto, fmt)
            if dt.isValid():
                return dt

        return None

    def calcular_angulo(self, pt1, pt2):
        """
        Calcula el azimut en grados (0-360) del vector que va de
        *pt1* a *pt2* en el sistema de coordenadas geográfico.

        Nota: al trabajar en coordenadas geográficas el resultado es
        aproximado; para cálculos de alta precisión habría que
        proyectar antes los puntos.
        """
        dx = pt2.x() - pt1.x()
        dy = pt2.y() - pt1.y()
        ang = math.degrees(math.atan2(dy, dx))
        return ang + 360 if ang < 0 else ang

    def diferencia_angular(self, a1, a2):
        """
        Devuelve la diferencia angular mínima (0-180°) entre dos
        azimuts *a1* y *a2*, teniendo en cuenta la circularidad de los
        360°.
        """
        diff = abs(a1 - a2)
        return min(diff, 360 - diff)

    def obtener_punto(self, geom):
        """
        Extrae el primer QgsPointXY de una geometría de punto simple
        o multipunto.

        Devuelve None si la geometría es nula o vacía.
        """
        if geom is None or geom.isEmpty():
            return None

        if QgsWkbTypes.isMultiType(geom.wkbType()):
            pts = geom.asMultiPoint()
            return pts[0] if pts else None

        return geom.asPoint()

    def normalizar_nombre(self, texto):
        """
        Convierte *texto* a minúsculas, sustituye espacios por
        guiones bajos y elimina tildes y eñes.

        Se usa para comparar nombres de artes de pesca de forma
        robusta independientemente de mayúsculas o acentuación.
        """
        SUSTITUCIONES = str.maketrans(
            "áéíóúñ ÁÉÍÓÚÑ",
            "aeioun_aeioun"
        )
        return texto.lower().strip().translate(SUSTITUCIONES)

    def obtener_rango_anios_datos(self, capa, campo_fecha):
        """
        Examina todos los registros de *capa* y devuelve una cadena
        con el rango de años presentes en *campo_fecha*.

        Ejemplos de salida: «2023», «2021_2024», «sin_anio».
        Se usa para construir el nombre base del fichero de salida.
        """
        anios = set()

        for feat in capa.getFeatures():
            dt = self.convertir_a_qdatetime(feat[campo_fecha])
            if dt is not None and dt.isValid():
                anios.add(dt.date().year())

        if not anios:
            return "sin_anio"

        anios_sorted = sorted(anios)
        return str(anios_sorted[0]) if len(anios_sorted) == 1 else f"{anios_sorted[0]}_{anios_sorted[-1]}"

    def obtener_barcos(self, capa, campo_barco):
        """
        Devuelve una lista ordenada con los valores únicos del campo
        *campo_barco* presentes en *capa*.

        Descarta cadenas vacías y devuelve lista vacía si el campo
        no existe.
        """
        idx = capa.fields().indexFromName(campo_barco)
        if idx == -1:
            return []

        return sorted(
            str(v).strip()
            for v in capa.uniqueValues(idx)
            if str(v).strip()
        )

    def crear_request_barco(self, campo_barco, barco):
        """
        Construye un QgsFeatureRequest que filtra los registros cuyo
        campo *campo_barco* sea igual a *barco*.

        Usa QgsExpression.quotedValue para escapar correctamente el
        valor y evitar inyecciones de expresión.
        """
        expr = f'"{campo_barco}" = {QgsExpression.quotedValue(barco)}'
        return QgsFeatureRequest().setFilterExpression(expr)

    # ------------------------------------------------------------------
    # CREACIÓN DE CAPAS DE SALIDA EN MEMORIA
    # ------------------------------------------------------------------

    def crear_capa_puntos_salida(self, capa_origen, nombre):
        """
        Crea una capa de puntos en memoria con el mismo CRS y los
        mismos campos que *capa_origen*, añadiendo el campo «jornada_»
        si no existe ya.

        Esta capa acumulará todos los puntos procesados con su
        identificador de jornada asignado.
        """
        capa = QgsVectorLayer(
            f"Point?crs={capa_origen.crs().authid()}", nombre, "memory"
        )
        prov = capa.dataProvider()
        prov.addAttributes(capa_origen.fields())

        # Añade «jornada_» solo si no estaba ya en los campos de origen.
        if "jornada" not in [f.name() for f in capa_origen.fields()]:
            prov.addAttributes([QgsField("jornada_", QVariant.String, len=400)])

        capa.updateFields()
        return capa

    def crear_capa_lineas_jornada_salida(self, crs_authid, nombre):
        """
        Crea una capa de líneas en memoria para almacenar la
        trayectoria completa de cada jornada.

        Campos:
        - barco, jornada, fecha_ini, fecha_fin
        - tiempo_min, tiempo_h : duración de la jornada
        - long_m               : longitud total (metros, EPSG:3035)
        - vel_kmh, vel_nudos   : velocidad media
        - num_puntos           : número de puntos GPS de la jornada
        """
        capa = QgsVectorLayer(
            f"LineString?crs={crs_authid}", nombre, "memory"
        )
        prov = capa.dataProvider()
        prov.addAttributes([
            QgsField("barco",      QVariant.String, len=200),
            QgsField("jornada",    QVariant.String, len=400),
            QgsField("fecha_ini",  QVariant.String, len=50),
            QgsField("fecha_fin",  QVariant.String, len=50),
            QgsField("tiempo_min", QVariant.Double),
            QgsField("tiempo_h",   QVariant.Double),
            QgsField("long_m",     QVariant.Double),
            QgsField("vel_kmh",    QVariant.Double),
            QgsField("vel_nudos",  QVariant.Double),
            QgsField("num_puntos", QVariant.Int),
        ])
        capa.updateFields()
        return capa

    def crear_capa_lineas_segmentos_salida(self, crs_authid, nombre):
        """
        Crea una capa de líneas en memoria para almacenar los segmentos
        entre puntos GPS consecutivos dentro de la misma jornada.

        Campos adicionales respecto a la capa de jornada:
        - angulo    : azimut del segmento (0-360°)
        - delta_ant : diferencia angular con el segmento anterior
        - delta_sig : diferencia angular con el segmento siguiente
        - es_valida : 1 si cumple los criterios de tanda, 0 si no
        - tanda     : número de tanda dentro de la jornada (o NULL)
        - cod_tanda : código único de tanda (jornada + número)
        """
        capa = QgsVectorLayer(
            f"LineString?crs={crs_authid}", nombre, "memory"
        )
        prov = capa.dataProvider()
        prov.addAttributes([
            QgsField("barco",      QVariant.String, len=200),
            QgsField("jornada",    QVariant.String, len=400),
            QgsField("fecha_ini",  QVariant.String, len=50),
            QgsField("fecha_fin",  QVariant.String, len=50),
            QgsField("tiempo_min", QVariant.Double),
            QgsField("tiempo_h",   QVariant.Double),
            QgsField("long_m",     QVariant.Double),
            QgsField("vel_kmh",    QVariant.Double),
            QgsField("vel_nudos",  QVariant.Double),
            QgsField("angulo",     QVariant.Double),
            QgsField("delta_ant",  QVariant.Double),
            QgsField("delta_sig",  QVariant.Double),
            QgsField("es_valida",  QVariant.Int),
            QgsField("tanda",      QVariant.Int),
            QgsField("cod_tanda",  QVariant.String, len=450),
        ])
        capa.updateFields()
        return capa

    def crear_capa_tandas_salida(self, crs_authid, nombre):
        """
        Crea una capa de multilíneas en memoria para almacenar cada
        tanda de pesca detectada, formada por la unión de los segmentos
        válidos consecutivos.

        Campos adicionales:
        - x_ini, y_ini : coordenadas del punto inicial de la tanda
        - x_fin, y_fin : coordenadas del punto final de la tanda
        - num_seg      : número de segmentos que componen la tanda
        """
        capa = QgsVectorLayer(
            f"MultiLineString?crs={crs_authid}", nombre, "memory"
        )
        prov = capa.dataProvider()
        prov.addAttributes([
            QgsField("barco",      QVariant.String, len=200),
            QgsField("jornada",    QVariant.String, len=400),
            QgsField("cod_tanda",  QVariant.String, len=450),
            QgsField("tanda",      QVariant.Int),
            QgsField("fecha_ini",  QVariant.String, len=50),
            QgsField("fecha_fin",  QVariant.String, len=50),
            QgsField("x_ini",      QVariant.Double),
            QgsField("y_ini",      QVariant.Double),
            QgsField("x_fin",      QVariant.Double),
            QgsField("y_fin",      QVariant.Double),
            QgsField("tiempo_min", QVariant.Double),
            QgsField("tiempo_h",   QVariant.Double),
            QgsField("long_m",     QVariant.Double),
            QgsField("vel_kmh",    QVariant.Double),
            QgsField("vel_nudos",  QVariant.Double),
            QgsField("num_seg",    QVariant.Int),
        ])
        capa.updateFields()
        return capa

    def copiar_features(self, capa_origen, capa_destino):
        """
        Copia todas las entidades de *capa_origen* a *capa_destino*,
        mapeando los atributos por nombre de campo.

        Los campos que no existen en el origen se rellenan con None.
        Al finalizar actualiza la extensión de la capa destino.
        """
        nuevas = []

        for feat in capa_origen.getFeatures():
            nueva = QgsFeature(capa_destino.fields())
            nueva.setGeometry(QgsGeometry(feat.geometry()))

            attrs = [
                feat[campo.name()]
                if feat.fields().indexFromName(campo.name()) != -1
                else None
                for campo in capa_destino.fields()
            ]
            nueva.setAttributes(attrs)
            nuevas.append(nueva)

        if nuevas:
            capa_destino.dataProvider().addFeatures(nuevas)
            capa_destino.updateExtents()

    # ------------------------------------------------------------------
    # RECONSTRUCCIÓN DE TRAYECTORIAS
    # ------------------------------------------------------------------

    def crear_lineas_jornada(self, capa_puntos, campo_barco, campo_fecha):
        """
        Une los puntos GPS de cada jornada en una polilínea.

        Proceso:
        1. Agrupa los puntos por (barco, jornada) y los ordena por fecha.
        2. Elimina puntos duplicados (mismas coordenadas consecutivas).
        3. Crea la geometría de línea y calcula longitud en metros (EPSG:3035),
           duración y velocidad media.
        4. Devuelve la capa de líneas de jornada en memoria.
        """
        capa_salida = self.crear_capa_lineas_jornada_salida(
            capa_puntos.crs().authid(), "lineas_jornada_tmp"
        )
        prov = capa_salida.dataProvider()

        # Transformación al CRS métrico para el cálculo de longitudes.
        crs_entrada = capa_puntos.crs()
        crs_calculo = QgsCoordinateReferenceSystem("EPSG:3035")
        transformacion = QgsCoordinateTransform(
            crs_entrada, crs_calculo, QgsProject.instance()
        )

        # --- Agrupación de puntos por (barco, jornada) ---
        grupos = {}

        for feat in capa_puntos.getFeatures():
            barco   = str(feat[campo_barco]).strip()
            jornada = str(feat["jornada_"]).strip()
            dt      = self.convertir_a_qdatetime(feat[campo_fecha])
            pt      = self.obtener_punto(feat.geometry())

            if dt is None or not dt.isValid() or pt is None:
                continue

            clave = (barco, jornada)
            grupos.setdefault(clave, []).append({"pt": pt, "dt": dt})

        # --- Construcción de líneas por jornada ---
        nuevas = []

        for (barco, jornada), lista in grupos.items():
            lista.sort(key=lambda x: x["dt"].toSecsSinceEpoch())

            # Elimina puntos duplicados consecutivos.
            puntos = []
            ultimo = None
            for item in lista:
                pt = item["pt"]
                if ultimo is None or pt.x() != ultimo.x() or pt.y() != ultimo.y():
                    puntos.append(pt)
                    ultimo = pt

            if len(puntos) < 2:
                continue

            # Geometría en CRS original para visualización.
            geom_linea = QgsGeometry.fromPolylineXY(puntos)

            # Geometría proyectada para cálculo de longitud en metros.
            geom_calc = QgsGeometry.fromPolylineXY(puntos)
            geom_calc.transform(transformacion)
            long_m = geom_calc.length()

            dt_ini = lista[0]["dt"]
            dt_fin = lista[-1]["dt"]
            seg    = dt_ini.secsTo(dt_fin)

            tiempo_min = seg / 60.0   if seg > 0 else None
            tiempo_h   = seg / 3600.0 if seg > 0 else None

            if tiempo_h:
                vel_kmh   = (long_m / 1000.0) / tiempo_h
                vel_nudos = vel_kmh / 1.852
            else:
                vel_kmh = vel_nudos = None

            nueva = QgsFeature(capa_salida.fields())
            nueva.setGeometry(geom_linea)
            nueva.setAttributes([
                barco,
                jornada,
                dt_ini.toString("yyyy-MM-dd HH:mm:ss"),
                dt_fin.toString("yyyy-MM-dd HH:mm:ss"),
                tiempo_min,
                tiempo_h,
                long_m,
                vel_kmh,
                vel_nudos,
                len(puntos),
            ])
            nuevas.append(nueva)

        if nuevas:
            prov.addFeatures(nuevas)

        capa_salida.updateExtents()
        return capa_salida

    def crear_lineas_segmentos(self, capa_puntos, campo_barco, campo_fecha):
        """
        Genera un segmento de línea (2 puntos) entre cada par de puntos
        GPS consecutivos de la misma jornada y barco.

        Para cada segmento calcula:
        - Longitud en metros (EPSG:3035)
        - Tiempo transcurrido y velocidad media
        - Azimut (ángulo de rumbo)

        Los campos de análisis de tanda (delta_ant, delta_sig,
        es_valida, tanda, cod_tanda) se dejan a None; se rellenan
        después en ``detectar_tandas``.
        """
        capa_lineas = self.crear_capa_lineas_segmentos_salida(
            capa_puntos.crs().authid(), "lineas_segmentos_tmp"
        )
        prov = capa_lineas.dataProvider()

        crs_entrada = capa_puntos.crs()
        crs_calculo = QgsCoordinateReferenceSystem("EPSG:3035")
        transformacion = QgsCoordinateTransform(
            crs_entrada, crs_calculo, QgsProject.instance()
        )

        # --- Lectura y ordenación global de todos los puntos ---
        puntos_preparados = []

        for feat in capa_puntos.getFeatures():
            barco   = str(feat[campo_barco]).strip()
            jornada = str(feat["jornada_"]).strip()
            dt      = self.convertir_a_qdatetime(feat[campo_fecha])
            pt      = self.obtener_punto(feat.geometry())

            if dt is None or not dt.isValid() or pt is None:
                continue

            puntos_preparados.append({
                "barco": barco, "jornada": jornada, "dt": dt, "pt": pt
            })

        puntos_preparados.sort(
            key=lambda x: (x["barco"], x["jornada"], x["dt"].toSecsSinceEpoch())
        )

        # --- Creación de segmentos entre puntos consecutivos ---
        nuevas = []

        for i in range(len(puntos_preparados) - 1):
            p1_item = puntos_preparados[i]
            p2_item = puntos_preparados[i + 1]

            # Solo se conectan puntos del mismo barco y jornada.
            if (p1_item["barco"]   != p2_item["barco"] or
                    p1_item["jornada"] != p2_item["jornada"]):
                continue

            p1 = p1_item["pt"]
            p2 = p2_item["pt"]

            geom_linea = QgsGeometry.fromPolylineXY([p1, p2])
            geom_calc  = QgsGeometry.fromPolylineXY([p1, p2])
            geom_calc.transform(transformacion)
            long_m = geom_calc.length()

            dt1 = p1_item["dt"]
            dt2 = p2_item["dt"]
            seg = dt1.secsTo(dt2)

            tiempo_min = seg / 60.0   if seg > 0 else None
            tiempo_h   = seg / 3600.0 if seg > 0 else None

            if tiempo_h:
                vel_kmh   = (long_m / 1000.0) / tiempo_h
                vel_nudos = vel_kmh / 1.852
            else:
                vel_kmh = vel_nudos = None

            angulo = self.calcular_angulo(p1, p2)

            nueva = QgsFeature(capa_lineas.fields())
            nueva.setGeometry(geom_linea)
            nueva.setAttributes([
                p1_item["barco"], p1_item["jornada"],
                dt1.toString("yyyy-MM-dd HH:mm:ss"),
                dt2.toString("yyyy-MM-dd HH:mm:ss"),
                tiempo_min, tiempo_h, long_m,
                vel_kmh, vel_nudos,
                angulo,
                None, None, None, None, None,   # delta_ant/sig, es_valida, tanda, cod_tanda
            ])
            nuevas.append(nueva)

        if nuevas:
            prov.addFeatures(nuevas)

        capa_lineas.updateExtents()
        return capa_lineas

    # ------------------------------------------------------------------
    # DETECCIÓN DE TANDAS DE PESCA
    # ------------------------------------------------------------------

    def detectar_tandas(
        self,
        capa_lineas,
        arte_txt,
        umbral_nudos_min,
        umbral_nudos,
        umbral_distancia,
        umbral_max_tanda,
        min_lineas_tanda,
        umbral_cambio_angulo,
        umbral_longitud_total_tanda,
    ):
        """
        Analiza los segmentos de *capa_lineas* y marca los que forman
        parte de una tanda de pesca.

        Algoritmo en tres pasos
        -----------------------
        1. **Clasificación individual**: cada segmento se marca como
           válido o no según velocidad, longitud y cambio angular.

        2. **Relleno de huecos**: secuencias cortas de segmentos no
           válidos (≤ MAX_HUECO) flanqueadas por suficientes segmentos
           válidos (≥ MIN_VALIDOS_POR_LADO) se marcan como válidos.
           Esto evita que ruido puntual rompa tandas largas.

        3. **Formación de tandas**: se recorren los segmentos en orden
           y se agrupan las rachas de segmentos válidos consecutivos.
           Una racha se convierte en tanda solo si tiene al menos
           *min_lineas_tanda* segmentos Y su longitud total no supera
           *umbral_longitud_total_tanda*.

        Umbral dinámico de velocidad
        ----------------------------
        Si el JSON del arte tiene ``"umbral_dinamico": true``, el
        umbral superior de velocidad se ajusta al máximo entre el
        valor fijo y 1.3 × la velocidad media de todos los segmentos.

        Los resultados se escriben directamente en los campos
        ``delta_ant``, ``delta_sig``, ``es_valida``, ``tanda`` y
        ``cod_tanda`` de *capa_lineas* mediante ``changeAttributeValues``.

        Parámetros
        ----------
        capa_lineas              : capa de segmentos a analizar
        arte_txt                 : nombre normalizado del arte de pesca
        umbral_nudos_min         : velocidad mínima en nudos para pesca
        umbral_nudos             : velocidad máxima en nudos para pesca
        umbral_distancia         : longitud máxima (m) de un segmento
                                   corto que se acepta sin filtro de velocidad
        umbral_max_tanda         : longitud máxima absoluta de un segmento
        min_lineas_tanda         : número mínimo de segmentos para tanda
        umbral_cambio_angulo     : cambio angular máximo permitido (°)
        umbral_longitud_total_tanda : longitud acumulada máxima de una tanda (m)
        """

        # Constantes internas de relleno de huecos.
        MIN_VALIDOS_POR_LADO = 3   # Segmentos válidos mínimos a cada lado del hueco.
        MAX_HUECO            = 3   # Máximo de segmentos no válidos seguidos a rellenar.

        # Conversión explícita de tipos para evitar errores en comparaciones.
        UMBRAL_NUDOS_MIN               = float(umbral_nudos_min)
        UMBRAL_NUDOS                   = float(umbral_nudos)
        UMBRAL_DISTANCIA_M             = float(umbral_distancia)
        UMBRAL_MAXIMO_TANDA_M          = float(umbral_max_tanda)
        MIN_LINEAS_TANDA               = int(min_lineas_tanda)
        UMBRAL_CAMBIO_ANGULO           = float(umbral_cambio_angulo)
        UMBRAL_LONGITUD_TOTAL_TANDA_M  = float(umbral_longitud_total_tanda)

        # La capa debe estar en modo edición para poder modificar atributos.
        if not capa_lineas.isEditable():
            capa_lineas.startEditing()

        # Índices de los campos que se van a leer o escribir.
        idx = {
            nombre: capa_lineas.fields().indexFromName(nombre)
            for nombre in [
                "barco", "jornada", "fecha_ini", "long_m", "vel_nudos",
                "angulo", "delta_ant", "delta_sig", "es_valida", "tanda", "cod_tanda",
            ]
        }

        # --- Carga de todos los segmentos en memoria ---
        lineas = []

        for feat in capa_lineas.getFeatures():
            dt = self.convertir_a_qdatetime(feat[idx["fecha_ini"]])
            if dt is None or not dt.isValid():
                continue

            lineas.append({
                "fid":       feat.id(),
                "barco":     str(feat[idx["barco"]]).strip(),
                "jornada":   str(feat[idx["jornada"]]).strip(),
                "fecha_ini": dt,
                "long_m":    self.valor_float_seguro(feat[idx["long_m"]]),
                "vel_nudos": self.valor_float_seguro(feat[idx["vel_nudos"]]),
                "angulo":    self.valor_float_seguro(feat[idx["angulo"]]),
                "delta_ant": None,
                "delta_sig": None,
                "es_valida": False,
                "tanda":     None,
                "cod_tanda": None,
            })

        lineas.sort(
            key=lambda x: (x["barco"], x["jornada"], x["fecha_ini"].toSecsSinceEpoch())
        )

        # ── UMBRAL DINÁMICO DE VELOCIDAD ─────────────────────────────
        # Si el JSON del arte activa «umbral_dinamico», el umbral
        # superior se recalcula como max(fijo, media × 1.3).
        UMBRAL_NUDOS_DINAMICO = UMBRAL_NUDOS
        usar_umbral_dinamico  = bool(
            self.parametros_artes.get(arte_txt, {}).get("umbral_dinamico", False)
        )

        if usar_umbral_dinamico:
            vels = [l["vel_nudos"] for l in lineas if l["vel_nudos"] > 0]
            if vels:
                vel_media             = sum(vels) / len(vels)
                UMBRAL_NUDOS_DINAMICO = max(UMBRAL_NUDOS, vel_media * 1.3)
            self.log(
                f"Umbral dinámico activado para {arte_txt}: "
                f"{round(UMBRAL_NUDOS_DINAMICO, 2)} nudos"
            )
        else:
            self.log(f"Umbral fijo para {arte_txt}: {round(UMBRAL_NUDOS, 2)} nudos")

        # ── PASO 1: Clasificación individual de segmentos ─────────────
        for i, lin in enumerate(lineas):
            grupo = (lin["barco"], lin["jornada"])

            # Diferencia angular con el segmento anterior (mismo grupo).
            delta_ant = None
            if i > 0:
                ant = lineas[i - 1]
                if (ant["barco"], ant["jornada"]) == grupo:
                    delta_ant = self.diferencia_angular(lin["angulo"], ant["angulo"])

            # Diferencia angular con el segmento siguiente (mismo grupo).
            delta_sig = None
            if i < len(lineas) - 1:
                sig = lineas[i + 1]
                if (sig["barco"], sig["jornada"]) == grupo:
                    delta_sig = self.diferencia_angular(lin["angulo"], sig["angulo"])

            lin["delta_ant"] = delta_ant
            lin["delta_sig"] = delta_sig

            # Un cambio brusco de ángulo invalida el segmento.
            cambio_brusco = (
                (delta_ant is not None and delta_ant > UMBRAL_CAMBIO_ANGULO) or
                (delta_sig is not None and delta_sig > UMBRAL_CAMBIO_ANGULO)
            )

            # Criterio de validez:
            # - La longitud no supera el máximo absoluto.
            # - O bien la velocidad está en el rango [min, max],
            #   o bien el segmento es muy corto (≤ umbral_distancia).
            en_rango_velocidad = (
                UMBRAL_NUDOS_MIN <= lin["vel_nudos"] <= UMBRAL_NUDOS_DINAMICO
            )
            segmento_corto = lin["long_m"] <= UMBRAL_DISTANCIA_M

            lin["es_valida"] = (
                not cambio_brusco
                and lin["long_m"] <= UMBRAL_MAXIMO_TANDA_M
                and (en_rango_velocidad or segmento_corto)
            )

        # ── PASO 2: Relleno de huecos pequeños ───────────────────────
        # Recorre la lista buscando segmentos no válidos. Si el hueco es
        # pequeño y tiene suficientes válidos a ambos lados, se rellena.
        i = 0
        while i < len(lineas):
            lin = lineas[i]

            if lin["es_valida"]:
                i += 1
                continue

            grupo          = (lin["barco"], lin["jornada"])
            inicio_hueco   = i
            fin_hueco      = i

            # Extiende el hueco mientras haya no válidos del mismo grupo.
            while (
                fin_hueco + 1 < len(lineas)
                and not lineas[fin_hueco + 1]["es_valida"]
                and (lineas[fin_hueco + 1]["barco"], lineas[fin_hueco + 1]["jornada"]) == grupo
            ):
                fin_hueco += 1

            tam_hueco = fin_hueco - inicio_hueco + 1

            # Cuenta válidos inmediatamente antes del hueco.
            validas_antes = 0
            j = inicio_hueco - 1
            while j >= 0:
                ant = lineas[j]
                if (ant["barco"], ant["jornada"]) != grupo or not ant["es_valida"]:
                    break
                validas_antes += 1
                j -= 1

            # Cuenta válidos inmediatamente después del hueco.
            validas_despues = 0
            j = fin_hueco + 1
            while j < len(lineas):
                sig = lineas[j]
                if (sig["barco"], sig["jornada"]) != grupo or not sig["es_valida"]:
                    break
                validas_despues += 1
                j += 1

            # Rellena el hueco si cumple los umbrales.
            if (
                tam_hueco       <= MAX_HUECO and
                validas_antes   >= MIN_VALIDOS_POR_LADO and
                validas_despues >= MIN_VALIDOS_POR_LADO
            ):
                for k in range(inicio_hueco, fin_hueco + 1):
                    lineas[k]["es_valida"] = True

            i = fin_hueco + 1

        # ── PASO 3: Formación de tandas ───────────────────────────────
        def cerrar_racha(lista_lineas, inicio, longitud, tanda_num, jornada_val):
            """
            Cierra una racha de segmentos válidos y la convierte en
            tanda si supera el mínimo de segmentos y no supera la
            longitud total máxima.

            Asigna ``tanda`` y ``cod_tanda`` a cada segmento de la racha
            y devuelve el número de tanda actualizado.
            """
            if inicio is None or longitud < MIN_LINEAS_TANDA:
                return tanda_num

            # Calcula la longitud acumulada de la racha.
            long_total = sum(
                self.valor_float_seguro(lista_lineas[j]["long_m"])
                for j in range(inicio, inicio + longitud)
            )

            # Descarta la racha si supera la longitud total permitida.
            if long_total > UMBRAL_LONGITUD_TOTAL_TANDA_M:
                return tanda_num

            tanda_num += 1
            for j in range(inicio, inicio + longitud):
                lista_lineas[j]["tanda"]     = tanda_num
                lista_lineas[j]["cod_tanda"] = f"{jornada_val}00{tanda_num:02d}"

            return tanda_num

        grupo_actual   = None
        jornada_actual = None
        tanda_actual   = 0
        racha_inicio   = None
        racha_longitud = 0

        for i, lin in enumerate(lineas):
            grupo = (lin["barco"], lin["jornada"])

            # Al cambiar de grupo cerramos la racha anterior si la hubiera.
            if grupo != grupo_actual:
                if grupo_actual is not None:
                    tanda_actual = cerrar_racha(
                        lineas, racha_inicio, racha_longitud,
                        tanda_actual, jornada_actual
                    )
                grupo_actual   = grupo
                jornada_actual = lin["jornada"]
                tanda_actual   = 0
                racha_inicio   = None
                racha_longitud = 0

            if lin["es_valida"]:
                if racha_inicio is None:
                    racha_inicio   = i
                    racha_longitud = 1
                else:
                    racha_longitud += 1
            else:
                tanda_actual = cerrar_racha(
                    lineas, racha_inicio, racha_longitud,
                    tanda_actual, jornada_actual
                )
                racha_inicio   = None
                racha_longitud = 0

        # Cierra la última racha del último grupo.
        if lineas:
            cerrar_racha(
                lineas, racha_inicio, racha_longitud,
                tanda_actual, jornada_actual
            )

        # --- Escritura de resultados en la capa ---
        cambios = {
            lin["fid"]: {
                idx["delta_ant"]:  lin["delta_ant"],
                idx["delta_sig"]:  lin["delta_sig"],
                idx["es_valida"]:  1 if lin["es_valida"] else 0,
                idx["tanda"]:      lin["tanda"],
                idx["cod_tanda"]:  lin["cod_tanda"],
            }
            for lin in lineas
        }

        if cambios:
            capa_lineas.dataProvider().changeAttributeValues(cambios)

        capa_lineas.commitChanges()
        return capa_lineas

    # ------------------------------------------------------------------
    # UNIÓN DE SEGMENTOS EN TANDAS
    # ------------------------------------------------------------------

    def unir_tandas(self, capa_lineas):
        """
        Agrupa los segmentos de *capa_lineas* por ``cod_tanda`` y crea
        una entidad multilínea por cada tanda, calculando los estadísticos
        acumulados (longitud, tiempo, velocidad media) y las coordenadas
        de inicio y fin de la tanda.

        Devuelve la capa de tandas en memoria. Las entidades sin tanda
        asignada (campo ``cod_tanda`` nulo o vacío) se ignoran.
        """
        capa_salida = self.crear_capa_tandas_salida(
            capa_lineas.crs().authid(), "tandas_tmp"
        )
        prov = capa_salida.dataProvider()

        # Índices de campos de la capa de segmentos.
        idx = {
            nombre: capa_lineas.fields().indexFromName(nombre)
            for nombre in [
                "barco", "jornada", "cod_tanda", "tanda",
                "fecha_ini", "fecha_fin", "tiempo_min", "tiempo_h", "long_m",
            ]
        }

        # --- Agrupación por (barco, jornada, cod_tanda) ---
        grupos = {}

        for feat in capa_lineas.getFeatures():
            cod_tanda = feat[idx["cod_tanda"]]
            tanda     = feat[idx["tanda"]]

            # Descarta segmentos sin tanda asignada.
            if not cod_tanda or str(cod_tanda).strip() in ("", "NULL", "QVariant()"):
                continue
            if tanda is None or str(tanda).strip() in ("", "NULL", "QVariant()"):
                continue

            barco     = str(feat[idx["barco"]]).strip()
            jornada   = str(feat[idx["jornada"]]).strip()
            cod_tanda = str(cod_tanda).strip()
            clave     = (barco, jornada, cod_tanda)

            grupos.setdefault(clave, []).append({
                "geom":      QgsGeometry(feat.geometry()),
                "barco":     barco,
                "jornada":   jornada,
                "cod_tanda": cod_tanda,
                "tanda":     self.valor_float_seguro(feat[idx["tanda"]]),
                "fecha_ini": self.convertir_a_qdatetime(feat[idx["fecha_ini"]]),
                "fecha_fin": self.convertir_a_qdatetime(feat[idx["fecha_fin"]]),
                "tiempo_min": self.valor_float_seguro(feat[idx["tiempo_min"]]),
                "tiempo_h":   self.valor_float_seguro(feat[idx["tiempo_h"]]),
                "long_m":     self.valor_float_seguro(feat[idx["long_m"]]),
            })

        # --- Construcción de la entidad de cada tanda ---
        nuevas = []

        for (barco, jornada, cod_tanda), lista in grupos.items():
            lista.sort(
                key=lambda x: x["fecha_ini"].toSecsSinceEpoch()
                if x["fecha_ini"] is not None else 0
            )

            geoms           = []
            long_total      = 0.0
            tiempo_total_min = 0.0
            tiempo_total_h   = 0.0
            fecha_ini_tanda  = None
            fecha_fin_tanda  = None

            for item in lista:
                g = item["geom"]
                if g is not None and not g.isEmpty():
                    geoms.append(g)
                    long_total       += item["long_m"]
                    tiempo_total_min += item["tiempo_min"]
                    tiempo_total_h   += item["tiempo_h"]

                # Rango temporal de la tanda.
                if item["fecha_ini"] is not None:
                    if fecha_ini_tanda is None or item["fecha_ini"] < fecha_ini_tanda:
                        fecha_ini_tanda = item["fecha_ini"]
                if item["fecha_fin"] is not None:
                    if fecha_fin_tanda is None or item["fecha_fin"] > fecha_fin_tanda:
                        fecha_fin_tanda = item["fecha_fin"]

            if not geoms:
                continue

            # Une todas las geometrías de segmento en una multilínea.
            geom_union = QgsGeometry.unaryUnion(geoms)
            if geom_union is None or geom_union.isEmpty():
                continue

            # Coordenadas de inicio y fin de la tanda.
            pts_ini = lista[0]["geom"].asPolyline()
            pts_fin = lista[-1]["geom"].asPolyline()
            x_ini   = pts_ini[0].x() if pts_ini else None
            y_ini   = pts_ini[0].y() if pts_ini else None
            x_fin   = pts_fin[-1].x() if pts_fin else None
            y_fin   = pts_fin[-1].y() if pts_fin else None

            # Velocidad media de la tanda.
            if tiempo_total_h > 0:
                vel_media_kmh   = (long_total / 1000.0) / tiempo_total_h
                vel_media_nudos = vel_media_kmh / 1.852
            else:
                vel_media_kmh = vel_media_nudos = None

            nueva = QgsFeature(capa_salida.fields())
            nueva.setGeometry(geom_union)
            nueva.setAttributes([
                barco, jornada, cod_tanda,
                int(self.valor_float_seguro(lista[0]["tanda"])),
                fecha_ini_tanda.toString("yyyy-MM-dd HH:mm:ss") if fecha_ini_tanda else None,
                fecha_fin_tanda.toString("yyyy-MM-dd HH:mm:ss") if fecha_fin_tanda else None,
                x_ini, y_ini, x_fin, y_fin,
                tiempo_total_min, tiempo_total_h,
                long_total, vel_media_kmh, vel_media_nudos,
                len(lista),
            ])
            nuevas.append(nueva)

        if nuevas:
            prov.addFeatures(nuevas)

        capa_salida.updateExtents()
        return capa_salida

    # ------------------------------------------------------------------
    # EXPORTACIÓN A GEOPACKAGE Y CARGA EN QGIS
    # ------------------------------------------------------------------

    def exportar_capa_a_gpkg(
        self, capa, ruta_gpkg, nombre_capa_gpkg, sobrescribir_archivo=False
    ):
        """
        Exporta *capa* al GeoPackage *ruta_gpkg* con el nombre de
        capa *nombre_capa_gpkg*.

        Si *sobrescribir_archivo* es True, recrea el fichero GeoPackage
        completo; si es False, añade o sobreescribe solo la capa
        indicada dentro del fichero existente.

        Devuelve True si la exportación fue correcta, False si hubo error.
        """
        if capa is None or not capa.isValid():
            self.log(f"Error: la capa '{nombre_capa_gpkg}' no es válida.")
            return False

        opciones                    = QgsVectorFileWriter.SaveVectorOptions()
        opciones.driverName         = "GPKG"
        opciones.layerName          = nombre_capa_gpkg
        opciones.fileEncoding       = "UTF-8"
        opciones.actionOnExistingFile = (
            QgsVectorFileWriter.CreateOrOverwriteFile
            if sobrescribir_archivo
            else QgsVectorFileWriter.CreateOrOverwriteLayer
        )

        res = QgsVectorFileWriter.writeAsVectorFormatV3(
            capa,
            ruta_gpkg,
            QgsProject.instance().transformContext(),
            opciones,
        )

        # writeAsVectorFormatV3 devuelve una tupla (código, mensaje).
        resultado = res[0] if isinstance(res, tuple) else res
        mensaje   = res[1] if isinstance(res, tuple) and len(res) > 1 else ""

        if resultado == QgsVectorFileWriter.NoError:
            self.log(f"Capa exportada correctamente: {nombre_capa_gpkg}")
            return True

        self.log(f"Error al exportar la capa: {nombre_capa_gpkg}")
        self.log(f"Código: {resultado} | Mensaje: {mensaje}")
        return False

    def exportar_resultados(
        self,
        ruta_salida,
        capa_puntos,
        capa_lineas_jornada,
        capa_lineas_segmentos,
        capa_tandas,
        nombre_base_salida,
        rango_anios,
    ):
        """
        Exporta las cuatro capas de resultados a un único GeoPackage.

        El nombre del fichero sigue el patrón
        ``resultados_<nombre_base>.gpkg``. Si el fichero ya existe, se
        añade un sufijo numérico incremental para evitar sobreescritura.

        Devuelve la ruta del fichero creado o None si hubo errores.
        """
        if not ruta_salida or not os.path.isdir(ruta_salida):
            self.log("La carpeta de salida no existe o no se indicó.")
            return None

        # Nombres de capa dentro del GeoPackage.
        nombre_puntos         = f"puntos_{nombre_base_salida}"
        nombre_lineas_jornada = f"lineas_jornada_{nombre_base_salida}"
        nombre_lineas         = f"lineas_segmentos_{nombre_base_salida}"
        nombre_tandas         = f"tandas_{nombre_base_salida}"

        # Construye un nombre de fichero único sin sobreescribir.
        nombre_base = f"resultados_{nombre_base_salida}"
        ruta_gpkg   = os.path.join(ruta_salida, f"{nombre_base}.gpkg")
        contador    = 1
        while os.path.exists(ruta_gpkg):
            ruta_gpkg = os.path.join(ruta_salida, f"{nombre_base}_{contador}.gpkg")
            contador += 1

        self.log("=== EXPORTACIÓN AUTOMÁTICA ===")
        self.log(f"Archivo de salida: {ruta_gpkg}")

        # La primera capa crea el fichero; las demás se añaden.
        ok1 = self.exportar_capa_a_gpkg(capa_puntos,          ruta_gpkg, nombre_puntos,         True)
        ok2 = self.exportar_capa_a_gpkg(capa_lineas_jornada,  ruta_gpkg, nombre_lineas_jornada, False)
        ok3 = self.exportar_capa_a_gpkg(capa_lineas_segmentos, ruta_gpkg, nombre_lineas,         False)
        ok4 = self.exportar_capa_a_gpkg(capa_tandas,          ruta_gpkg, nombre_tandas,         False)

        if all([ok1, ok2, ok3, ok4]):
            self.log("Exportación completada correctamente.")
            return ruta_gpkg

        self.log("La exportación terminó con errores.")
        return None

    def cargar_capa_desde_gpkg(self, ruta_gpkg, nombre_capa):
        """
        Carga una capa del GeoPackage en el proyecto QGIS activo y la
        añade al panel de capas.

        Devuelve la capa cargada o None si no es válida.
        """
        uri  = f"{ruta_gpkg}|layername={nombre_capa}"
        capa = QgsVectorLayer(uri, nombre_capa, "ogr")

        if not capa.isValid():
            self.log(f"No se pudo cargar la capa exportada: {nombre_capa}")
            return None

        QgsProject.instance().addMapLayer(capa)
        self.log(f"Capa cargada desde GeoPackage: {nombre_capa}")
        return capa

    # ------------------------------------------------------------------
    # PROCESO PRINCIPAL
    # ------------------------------------------------------------------

    def ejecutar_proceso(self):
        """
        Orquesta todo el flujo de procesamiento al pulsar «Ejecutar»:

        1. Lee parámetros del diálogo.
        2. Carga la capa de entrada.
        3. Itera sobre cada barco:
           a. Asigna jornadas a los puntos.
           b. Genera líneas de jornada.
           c. Genera segmentos y detecta tandas.
           d. Une segmentos en tandas.
           e. Acumula resultados en capas globales.
        4. Exporta las cuatro capas a un GeoPackage.
        5. Carga las capas exportadas en QGIS.
        6. Muestra estadísticas finales en el log.
        """
        self.dlg.txtLog.clear()
        self.dlg.progressBar.setValue(0)
        self.dlg.progressBar.setFormat("Preparando datos...")
        QCoreApplication.processEvents()

        # --- Lectura de parámetros del diálogo ---
        ruta_entrada  = self.dlg.txtRutaCsv.text().strip()
        arte          = self.dlg.cmbArtePesca.currentText()
        campo_barco   = self.dlg.cmbCampoBarco.currentText()
        campo_fecha   = self.dlg.cmbCampoFecha.currentText()
        ruta_salida   = self.dlg.txtRutaSalida.text().strip()

        umbral_nudos_min = (
            self.dlg.spnVelMinNudos.value()
            if hasattr(self.dlg, "spnVelMinNudos")
            else 0.0
        )
        umbral_nudos              = self.dlg.spnUmbralNudos.value()
        umbral_distancia          = self.dlg.spnUmbralDistancia.value()
        umbral_max_tanda          = self.dlg.spnUmbralMaxTanda.value()
        min_lineas_tanda          = self.dlg.spnMinLineasTanda.value()
        umbral_cambio_angulo      = self.dlg.spnUmbralAngulo.value()
        umbral_longitud_total_tanda = self.dlg.spnLongitudMaxTanda.value()

        # --- Cabecera del log ---
        self.log("=" * 38)
        self.log("        GEO PESCA - EJECUCIÓN")
        self.log("=" * 38)
        self.log(f"Entrada: {ruta_entrada}")
        self.log(f"Arte seleccionado: {arte}")
        self.log(f"Campo barco: {campo_barco}")
        self.log(f"Campo fecha: {campo_fecha}")
        self.log(f"Salida: {ruta_salida}")
        self.log("-" * 40 + " PARÁMETROS USADOS " + "-" * 40)
        self.log(f"  Velocidad mínima (nudos)          : {umbral_nudos_min}")
        self.log(f"  Velocidad máxima (nudos)          : {umbral_nudos}")
        self.log(f"  Distancia corta válida (m)        : {umbral_distancia}")
        self.log(f"  Longitud máx. segmento (m)        : {umbral_max_tanda}")
        self.log(f"  Mínimo segmentos por tanda        : {min_lineas_tanda}")
        self.log(f"  Cambio angular máx. (°)           : {umbral_cambio_angulo}")
        self.log(f"  Longitud máx. total tanda (m)     : {umbral_longitud_total_tanda}")
        self.log("-" * 99)

        # --- Carga de la capa de entrada ---
        capa_entrada = self.obtener_capa_entrada(ruta_entrada)
        if capa_entrada is None:
            self.log("Error cargando capa.")
            self.dlg.tabWidget.setCurrentIndex(2)
            return

        rango_anios = self.obtener_rango_anios_datos(capa_entrada, campo_fecha)
        arte_txt    = self.normalizar_nombre(arte)

        # Nombre base para capas y fichero de salida.
        nombre_usuario = self.dlg.txtNombreCapa.text().strip()
        nombre_base_salida = (
            self.normalizar_nombre(nombre_usuario)
            if nombre_usuario
            else f"{arte_txt}_{rango_anios}"
        )

        nombre_puntos         = f"puntos_{nombre_base_salida}"
        nombre_lineas_jornada = f"lineas_jornada_{nombre_base_salida}"
        nombre_lineas         = f"lineas_segmentos_{nombre_base_salida}"
        nombre_tandas         = f"tandas_{nombre_base_salida}"
        crs_authid            = capa_entrada.crs().authid()

        # --- Creación de capas acumuladoras en memoria ---
        capa_puntos_total         = self.crear_capa_puntos_salida(capa_entrada, nombre_puntos)
        capa_lineas_jornada_total = self.crear_capa_lineas_jornada_salida(crs_authid, nombre_lineas_jornada)
        capa_lineas_total         = self.crear_capa_lineas_segmentos_salida(crs_authid, nombre_lineas)
        capa_tandas_total         = self.crear_capa_tandas_salida(crs_authid, nombre_tandas)

        barcos = self.obtener_barcos(capa_entrada, campo_barco)
        if not barcos:
            self.log("No se encontraron barcos.")
            self.dlg.tabWidget.setCurrentIndex(2)
            return

        self.log(f"Barcos detectados: {len(barcos)}")

        total_barcos = len(barcos)
        self.dlg.progressBar.setMaximum(total_barcos)
        self.dlg.progressBar.setValue(0)
        self.dlg.progressBar.setFormat("Procesando barcos...")

        num_puntos = num_lineas_jornada = num_segmentos = num_tandas = 0

        # ── BUCLE PRINCIPAL POR BARCO ─────────────────────────────────
        for i, barco in enumerate(barcos, 1):
            self.dlg.progressBar.setValue(i)
            self.dlg.progressBar.setFormat(f"Procesando barco {i}/{total_barcos}")
            QCoreApplication.processEvents()

            # Log cada 5 barcos para no saturar el panel.
            if i == 1 or i == total_barcos or i % 5 == 0:
                self.log(f"[{i}/{total_barcos}] Procesando barco: {barco}")

            request = self.crear_request_barco(campo_barco, barco)

            # -- Asignación de jornadas a los puntos del barco --
            features_barco = []

            for feat in capa_entrada.getFeatures(request):
                dt        = self.convertir_a_qdatetime(feat[campo_fecha])
                barco_val = str(feat[campo_barco]).strip()

                if dt is None or not dt.isValid() or not barco_val:
                    if dt is None or not dt.isValid():
                        self.log(f"Fecha no válida en barco {barco}: {feat[campo_fecha]}")
                    continue

                # La jornada es la combinación de código de barco y fecha.
                jornada = f"{barco_val}_{dt.date().toString('yyyyMMdd')}"

                nueva = QgsFeature(capa_puntos_total.fields())
                nueva.setGeometry(QgsGeometry(feat.geometry()))

                attrs = [
                    jornada if campo.name() == "jornada_"
                    else feat[campo.name()] if feat.fields().indexFromName(campo.name()) != -1
                    else None
                    for campo in capa_puntos_total.fields()
                ]
                nueva.setAttributes(attrs)
                features_barco.append(nueva)

            if not features_barco:
                self.log(f"Barco {barco}: 0 puntos válidos")
                continue

            capa_puntos_total.dataProvider().addFeatures(features_barco)
            num_puntos += len(features_barco)

            # -- Capa temporal con solo los puntos del barco actual --
            capa_temp = QgsVectorLayer(f"Point?crs={crs_authid}", "tmp", "memory")
            prov_temp = capa_temp.dataProvider()
            prov_temp.addAttributes(capa_puntos_total.fields())
            capa_temp.updateFields()
            prov_temp.addFeatures(features_barco)
            capa_temp.updateExtents()

            # -- Líneas de jornada --
            capa_lineas_jornada_sub = self.crear_lineas_jornada(
                capa_temp, campo_barco, campo_fecha
            )
            if capa_lineas_jornada_sub.featureCount() > 0:
                num_lineas_jornada += capa_lineas_jornada_sub.featureCount()
                self.copiar_features(capa_lineas_jornada_sub, capa_lineas_jornada_total)

            # -- Segmentos y detección de tandas --
            capa_lineas_sub = self.crear_lineas_segmentos(
                capa_temp, campo_barco, campo_fecha
            )
            if capa_lineas_sub.featureCount() == 0:
                continue

            num_segmentos += capa_lineas_sub.featureCount()

            capa_lineas_sub = self.detectar_tandas(
                capa_lineas_sub,
                arte_txt,
                umbral_nudos_min,
                umbral_nudos,
                umbral_distancia,
                umbral_max_tanda,
                min_lineas_tanda,
                umbral_cambio_angulo,
                umbral_longitud_total_tanda,
            )
            self.copiar_features(capa_lineas_sub, capa_lineas_total)

            # -- Unión de segmentos en tandas --
            capa_tandas_sub = self.unir_tandas(capa_lineas_sub)
            if capa_tandas_sub.featureCount() > 0:
                num_tandas += capa_tandas_sub.featureCount()
                self.copiar_features(capa_tandas_sub, capa_tandas_total)

        # Actualiza extensiones de todas las capas acumuladoras.
        for capa in [
            capa_puntos_total,
            capa_lineas_jornada_total,
            capa_lineas_total,
            capa_tandas_total,
        ]:
            capa.updateExtents()

        # --- Exportación y carga en QGIS ---
        ruta_gpkg = self.exportar_resultados(
            ruta_salida,
            capa_puntos_total,
            capa_lineas_jornada_total,
            capa_lineas_total,
            capa_tandas_total,
            nombre_base_salida,
            rango_anios,
        )

        if ruta_gpkg:
            for nombre in [nombre_puntos, nombre_lineas_jornada, nombre_lineas, nombre_tandas]:
                self.cargar_capa_desde_gpkg(ruta_gpkg, nombre)

        # --- Resumen final ---
        self.log("-" * 40 + " RESULTADOS " + "-" * 40)
        self.log(f"  Puntos procesados          : {num_puntos}")
        self.log(f"  Líneas por jornada         : {num_lineas_jornada}")
        self.log(f"  Segmentos                  : {num_segmentos}")
        self.log(f"  Tandas detectadas          : {num_tandas}")
        self.dlg.progressBar.setValue(total_barcos)
        self.dlg.progressBar.setFormat("Proceso completado")
        QCoreApplication.processEvents()
        self.log("-" * 92)
        self.log("Proceso terminado.")
        self.dlg.tabWidget.setCurrentIndex(2)

    # ------------------------------------------------------------------
    # ARRANQUE DEL DIÁLOGO
    # ------------------------------------------------------------------

    def run(self):
        """
        Muestra el diálogo del plugin.

        En la primera ejecución crea el diálogo y conecta las señales.
        En ejecuciones posteriores reutiliza el diálogo existente,
        reiniciando solo la barra de progreso y el log.
        """
        if self.first_start is True:
            self.first_start = False
            self.dlg = geopescaDialog()

            # Conexión de botones y desplegables a sus métodos.
            self.dlg.btnBuscarCsv.clicked.connect(self.seleccionar_entrada)
            self.dlg.btnBuscarSalida.clicked.connect(self.seleccionar_salida)
            self.dlg.cmbArtePesca.currentIndexChanged.connect(self.aplicar_parametros_arte)
            self.dlg.button_box.button(QDialogButtonBox.Ok).clicked.connect(self.ejecutar_proceso)

            # Renombra los botones estándar al idioma del plugin.
            self.dlg.button_box.button(QDialogButtonBox.Ok).setText("Ejecutar")
            self.dlg.button_box.button(QDialogButtonBox.Cancel).setText("Cerrar")

        # Restablece estado visual del diálogo.
        self.dlg.progressBar.setValue(0)
        self.dlg.tabWidget.setCurrentIndex(0)
        self.dlg.txtLog.clear()

        # Recarga los parámetros del JSON y aplica los del arte activo.
        self.cargar_parametros_artes()
        self.aplicar_parametros_arte()

        self.dlg.show()
        self.dlg.exec_()