# -*- coding: utf-8 -*-

import math
import os
import json
from osgeo import ogr

from qgis.PyQt.QtCore import QSettings, QTranslator, QCoreApplication, QVariant, QDateTime
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QFileDialog, QDialogButtonBox, QInputDialog

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
    QgsSpatialIndex
)

from .resources import *
from .Geo_Pesca_dialog import geopescaDialog


class geopesca:

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)

        locale = QSettings().value("locale/userLocale")[0:2]
        locale_path = os.path.join(self.plugin_dir, "i18n", f"geopesca_{locale}.qm")

        if os.path.exists(locale_path):
            self.translator = QTranslator()
            self.translator.load(locale_path)
            QCoreApplication.installTranslator(self.translator)

        self.actions = []
        self.menu = self.tr(u"&GeoPesca")
        self.first_start = None
        self.subcapa_gpkg = None

        self.parametros_artes = {}
        self.cargar_parametros_artes()

    def tr(self, message):
        return QCoreApplication.translate("geopesca", message)

    def log(self, texto):
        try:
            self.dlg.txtLog.appendPlainText(str(texto))
        except Exception:
            pass

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
        parent=None
    ):
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
        icon_path = os.path.join(self.plugin_dir, "icon.png")
        self.add_action(
            icon_path,
            text=self.tr(u"GeoPesca"),
            callback=self.run,
            parent=self.iface.mainWindow()
        )
        self.first_start = True

    def unload(self):
        for action in self.actions:
            self.iface.removePluginMenu(self.tr(u"&GeoPesca"), action)
            self.iface.removeToolBarIcon(action)

    # ==========================================================
    # PARÁMETROS JSON
    # ==========================================================

    def cargar_parametros_artes(self):
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
        arte_original = self.dlg.cmbArtePesca.currentText()
        arte = self.normalizar_nombre(arte_original)

        if arte not in self.parametros_artes:
            self.log(f"No hay parámetros definidos para: {arte}")
            return

        p = self.parametros_artes[arte]

        if hasattr(self.dlg, "spnVelMinNudos"):
            self.dlg.spnVelMinNudos.setValue(float(p.get("nudos_min", 0.0)))

        self.dlg.spnUmbralNudos.setValue(float(p.get("nudos", 0.0)))
        self.dlg.spnUmbralDistancia.setValue(float(p.get("distancia", 0.0)))
        self.dlg.spnUmbralMaxTanda.setValue(float(p.get("max_tanda", 0.0)))
        self.dlg.spnMinLineasTanda.setValue(int(p.get("min_lineas", 1)))
        self.dlg.spnUmbralAngulo.setValue(float(p.get("angulo", 360.0)))
        self.dlg.spnLongitudMaxTanda.setValue(float(p.get("long_total", 999999999.0)))

        self.log(f"Parámetros cargados desde JSON para: {arte}")

    # ==========================================================
    # ENTRADA
    # ==========================================================

    def seleccionar_entrada(self):
        ruta, _ = QFileDialog.getOpenFileName(
            self.dlg,
            "Seleccionar archivo de entrada",
            "",
            "Archivos soportados (*.csv *.gpkg)"
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
        ruta = QFileDialog.getExistingDirectory(
            self.dlg,
            "Seleccionar carpeta de salida"
        )

        if ruta:
            self.dlg.txtRutaSalida.setText(ruta)
            self.log(f"Carpeta de salida seleccionada: {ruta}")

    def listar_subcapas_gpkg_puntos(self, ruta_gpkg):
        ds = ogr.Open(ruta_gpkg)

        if ds is None:
            self.log("No se pudo abrir el GeoPackage.")
            return []

        subcapas = []

        for i in range(ds.GetLayerCount()):
            lyr = ds.GetLayerByIndex(i)

            if lyr is None:
                continue

            geom_type = lyr.GetGeomType()

            if geom_type in (
                ogr.wkbPoint,
                ogr.wkbMultiPoint,
                ogr.wkbPoint25D,
                ogr.wkbMultiPoint25D
            ):
                subcapas.append(lyr.GetName())

        ds = None
        return subcapas

    def seleccionar_subcapa_gpkg(self, ruta_gpkg):
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
            False
        )

        if ok and subcapa:
            return subcapa

        return None

    def obtener_capa_entrada(self, ruta_entrada):
        extension = os.path.splitext(ruta_entrada)[1].lower()

        if extension == ".csv":
            uri = (
                "file:///" + ruta_entrada +
                "?type=csv"
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
        capa = self.obtener_capa_entrada(ruta_entrada)

        if capa is None:
            return

        campos = [field.name() for field in capa.fields()]

        self.dlg.cmbCampoBarco.clear()
        self.dlg.cmbCampoFecha.clear()

        self.dlg.cmbCampoBarco.addItems(campos)
        self.dlg.cmbCampoFecha.addItems(campos)

        for campo in campos:
            nombre = campo.lower()

            if "barco" in nombre or "embarc" in nombre or "cfr" in nombre:
                self.dlg.cmbCampoBarco.setCurrentText(campo)

            if (
                "time" in nombre or
                "fecha" in nombre or
                "hora" in nombre or
                "timstmp" in nombre or
                "datetime" in nombre
            ):
                self.dlg.cmbCampoFecha.setCurrentText(campo)

        self.log("Archivo cargado correctamente.")
        self.log(f"Campos detectados: {', '.join(campos)}")

    # ==========================================================
    # UTILIDADES
    # ==========================================================

    def valor_float_seguro(self, valor):
        if valor is None:
            return 0.0

        texto = str(valor).strip()

        if texto == "" or texto.upper() == "NULL" or texto == "QVariant()":
            return 0.0

        try:
            return float(valor)
        except Exception:
            try:
                return float(texto.replace(",", "."))
            except Exception:
                return 0.0

    def convertir_a_qdatetime(self, valor):
        if valor is None:
            return None

        if isinstance(valor, QDateTime) and valor.isValid():
            return valor

        texto = str(valor).strip()
        texto = texto.replace(" (UTC)", "")
        texto = texto.replace("(UTC)", "")
        texto = texto.replace("T", " ")
        texto = texto.replace("Z", "")
        texto = texto.strip()

        formatos_posibles = [
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

        for fmt in formatos_posibles:
            dt = QDateTime.fromString(texto, fmt)
            if dt.isValid():
                return dt

        return None

    def calcular_angulo(self, pt1, pt2):
        dx = pt2.x() - pt1.x()
        dy = pt2.y() - pt1.y()

        ang = math.degrees(math.atan2(dy, dx))

        if ang < 0:
            ang += 360

        return ang

    def diferencia_angular(self, a1, a2):
        diff = abs(a1 - a2)
        return min(diff, 360 - diff)

    def obtener_punto(self, geom):
        if geom is None or geom.isEmpty():
            return None

        if QgsWkbTypes.isMultiType(geom.wkbType()):
            pts = geom.asMultiPoint()
            if pts:
                return pts[0]
            return None

        return geom.asPoint()

    def normalizar_nombre(self, texto):
        texto = str(texto).lower().strip()
        texto = texto.replace(" ", "_")
        texto = texto.replace("á", "a")
        texto = texto.replace("é", "e")
        texto = texto.replace("í", "i")
        texto = texto.replace("ó", "o")
        texto = texto.replace("ú", "u")
        texto = texto.replace("ñ", "n")
        return texto

    def obtener_rango_anios_datos(self, capa, campo_fecha):
        anios = set()

        for feat in capa.getFeatures():
            dt = self.convertir_a_qdatetime(feat[campo_fecha])

            if dt is not None and dt.isValid():
                anios.add(dt.date().year())

        if not anios:
            return "sin_anio"

        anios = sorted(list(anios))

        if len(anios) == 1:
            return str(anios[0])

        return f"{anios[0]}_{anios[-1]}"

    def obtener_barcos(self, capa, campo_barco):
        idx = capa.fields().indexFromName(campo_barco)

        if idx == -1:
            return []

        barcos = []

        for valor in capa.uniqueValues(idx):
            barco = str(valor).strip()

            if barco and barco not in barcos:
                barcos.append(barco)

        return sorted(barcos)

    def crear_request_barco(self, campo_barco, barco):
        expr = f'"{campo_barco}" = {QgsExpression.quotedValue(barco)}'
        return QgsFeatureRequest().setFilterExpression(expr)

    # ==========================================================
    # CAPAS DE SALIDA
    # ==========================================================

    def crear_capa_puntos_salida(self, capa_origen, nombre):
        capa = QgsVectorLayer(
            f"Point?crs={capa_origen.crs().authid()}",
            nombre,
            "memory"
        )

        prov = capa.dataProvider()
        prov.addAttributes(capa_origen.fields())

        nombres = [f.name() for f in capa_origen.fields()]

        if "jornada" not in nombres:
            prov.addAttributes([
                QgsField("jornada_", QVariant.String, len=400)
            ])

        capa.updateFields()
        return capa

    def crear_capa_lineas_jornada_salida(self, crs_authid, nombre):
        capa = QgsVectorLayer(
            f"LineString?crs={crs_authid}",
            nombre,
            "memory"
        )

        prov = capa.dataProvider()

        prov.addAttributes([
            QgsField("barco", QVariant.String, len=200),
            QgsField("jornada", QVariant.String, len=400),
            QgsField("fecha_ini", QVariant.String, len=50),
            QgsField("fecha_fin", QVariant.String, len=50),
            QgsField("tiempo_min", QVariant.Double),
            QgsField("tiempo_h", QVariant.Double),
            QgsField("long_m", QVariant.Double),
            QgsField("vel_kmh", QVariant.Double),
            QgsField("vel_nudos", QVariant.Double),
            QgsField("num_puntos", QVariant.Int),
        ])

        capa.updateFields()
        return capa

    def crear_capa_lineas_segmentos_salida(self, crs_authid, nombre):
        capa = QgsVectorLayer(
            f"LineString?crs={crs_authid}",
            nombre,
            "memory"
        )

        prov = capa.dataProvider()

        prov.addAttributes([
            QgsField("barco", QVariant.String, len=200),
            QgsField("jornada", QVariant.String, len=400),
            QgsField("fecha_ini", QVariant.String, len=50),
            QgsField("fecha_fin", QVariant.String, len=50),
            QgsField("tiempo_min", QVariant.Double),
            QgsField("tiempo_h", QVariant.Double),
            QgsField("long_m", QVariant.Double),
            QgsField("vel_kmh", QVariant.Double),
            QgsField("vel_nudos", QVariant.Double),
            QgsField("angulo", QVariant.Double),
            QgsField("delta_ant", QVariant.Double),
            QgsField("delta_sig", QVariant.Double),
            QgsField("es_valida", QVariant.Int),
            QgsField("tanda", QVariant.Int),
            QgsField("cod_tanda", QVariant.String, len=450),
        ])

        capa.updateFields()
        return capa

    def crear_capa_tandas_salida(self, crs_authid, nombre):
        capa = QgsVectorLayer(
            f"MultiLineString?crs={crs_authid}",
            nombre,
            "memory"
        )

        prov = capa.dataProvider()

        prov.addAttributes([
            QgsField("barco", QVariant.String, len=200),
            QgsField("jornada", QVariant.String, len=400),
            QgsField("cod_tanda", QVariant.String, len=450),
            QgsField("tanda", QVariant.Int),
            QgsField("fecha_ini", QVariant.String, len=50),
            QgsField("fecha_fin", QVariant.String, len=50),
            QgsField("x_ini", QVariant.Double),
            QgsField("y_ini", QVariant.Double),
            QgsField("x_fin", QVariant.Double),
            QgsField("y_fin", QVariant.Double),
            QgsField("tiempo_min", QVariant.Double),
            QgsField("tiempo_h", QVariant.Double),
            QgsField("long_m", QVariant.Double),
            QgsField("vel_kmh", QVariant.Double),
            QgsField("vel_nudos", QVariant.Double),
            QgsField("num_seg", QVariant.Int),
        ])

        capa.updateFields()
        return capa

    def copiar_features(self, capa_origen, capa_destino):
        nuevas = []

        for feat in capa_origen.getFeatures():
            nueva = QgsFeature(capa_destino.fields())
            nueva.setGeometry(QgsGeometry(feat.geometry()))

            attrs = []
            for campo in capa_destino.fields():
                nombre = campo.name()
                if feat.fields().indexFromName(nombre) != -1:
                    attrs.append(feat[nombre])
                else:
                    attrs.append(None)

            nueva.setAttributes(attrs)
            nuevas.append(nueva)

        if nuevas:
            capa_destino.dataProvider().addFeatures(nuevas)
            capa_destino.updateExtents()

    # ==========================================================
    # CÁLCULOS
    # ==========================================================

    def crear_lineas_jornada(self, capa_puntos, campo_barco, campo_fecha):
        capa_salida = self.crear_capa_lineas_jornada_salida(
            capa_puntos.crs().authid(),
            "lineas_jornada_tmp"
        )

        prov = capa_salida.dataProvider()

        crs_entrada = capa_puntos.crs()
        crs_calculo = QgsCoordinateReferenceSystem("EPSG:3035")
        transformacion = QgsCoordinateTransform(crs_entrada, crs_calculo, QgsProject.instance())

        grupos = {}

        for feat in capa_puntos.getFeatures():
            barco = str(feat[campo_barco]).strip()
            jornada = str(feat["jornada_"]).strip()
            dt = self.convertir_a_qdatetime(feat[campo_fecha])

            if dt is None or not dt.isValid():
                continue

            pt = self.obtener_punto(feat.geometry())

            if pt is None:
                continue

            clave = (barco, jornada)

            if clave not in grupos:
                grupos[clave] = []

            grupos[clave].append({
                "pt": pt,
                "dt": dt
            })

        nuevas = []

        for clave, lista in grupos.items():
            barco, jornada = clave
            lista.sort(key=lambda x: x["dt"].toSecsSinceEpoch())

            puntos = []
            ultimo = None

            for item in lista:
                pt = item["pt"]

                if ultimo is None or pt.x() != ultimo.x() or pt.y() != ultimo.y():
                    puntos.append(pt)
                    ultimo = pt

            if len(puntos) < 2:
                continue

            geom_linea = QgsGeometry.fromPolylineXY(puntos)
            geom_calc = QgsGeometry.fromPolylineXY(puntos)
            geom_calc.transform(transformacion)

            long_m = geom_calc.length()

            dt_ini = lista[0]["dt"]
            dt_fin = lista[-1]["dt"]

            seg = dt_ini.secsTo(dt_fin)

            tiempo_min = seg / 60.0 if seg > 0 else None
            tiempo_h = seg / 3600.0 if seg > 0 else None

            if tiempo_h is not None and tiempo_h > 0:
                vel_kmh = (long_m / 1000.0) / tiempo_h
                vel_nudos = vel_kmh / 1.852
            else:
                vel_kmh = None
                vel_nudos = None

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
                len(puntos)
            ])
            nuevas.append(nueva)

        if nuevas:
            prov.addFeatures(nuevas)

        capa_salida.updateExtents()
        return capa_salida

    def crear_lineas_segmentos(self, capa_puntos, campo_barco, campo_fecha):
        capa_lineas = self.crear_capa_lineas_segmentos_salida(
            capa_puntos.crs().authid(),
            "lineas_segmentos_tmp"
        )

        prov = capa_lineas.dataProvider()

        crs_entrada = capa_puntos.crs()
        crs_calculo = QgsCoordinateReferenceSystem("EPSG:3035")
        transformacion = QgsCoordinateTransform(crs_entrada, crs_calculo, QgsProject.instance())

        puntos_preparados = []

        for feat in capa_puntos.getFeatures():
            barco = str(feat[campo_barco]).strip()
            jornada = str(feat["jornada_"]).strip()
            dt = self.convertir_a_qdatetime(feat[campo_fecha])
            pt = self.obtener_punto(feat.geometry())

            if dt is None or not dt.isValid() or pt is None:
                continue

            puntos_preparados.append({
                "barco": barco,
                "jornada": jornada,
                "dt": dt,
                "pt": pt
            })

        puntos_preparados.sort(
            key=lambda x: (
                x["barco"],
                x["jornada"],
                x["dt"].toSecsSinceEpoch()
            )
        )

        nuevas = []

        for i in range(len(puntos_preparados) - 1):
            p1_item = puntos_preparados[i]
            p2_item = puntos_preparados[i + 1]

            if p1_item["barco"] != p2_item["barco"]:
                continue

            if p1_item["jornada"] != p2_item["jornada"]:
                continue

            p1 = p1_item["pt"]
            p2 = p2_item["pt"]

            geom_linea = QgsGeometry.fromPolylineXY([p1, p2])

            geom_calc = QgsGeometry.fromPolylineXY([p1, p2])
            geom_calc.transform(transformacion)

            long_m = geom_calc.length()

            dt1 = p1_item["dt"]
            dt2 = p2_item["dt"]

            seg = dt1.secsTo(dt2)

            tiempo_min = seg / 60.0 if seg > 0 else None
            tiempo_h = seg / 3600.0 if seg > 0 else None

            if tiempo_h is not None and tiempo_h > 0:
                vel_kmh = (long_m / 1000.0) / tiempo_h
                vel_nudos = vel_kmh / 1.852
            else:
                vel_kmh = None
                vel_nudos = None

            angulo = self.calcular_angulo(p1, p2)

            nueva = QgsFeature(capa_lineas.fields())
            nueva.setGeometry(geom_linea)
            nueva.setAttributes([
                p1_item["barco"],
                p1_item["jornada"],
                dt1.toString("yyyy-MM-dd HH:mm:ss"),
                dt2.toString("yyyy-MM-dd HH:mm:ss"),
                tiempo_min,
                tiempo_h,
                long_m,
                vel_kmh,
                vel_nudos,
                angulo,
                None,
                None,
                None,
                None,
                None
            ])

            nuevas.append(nueva)

        if nuevas:
            prov.addFeatures(nuevas)

        capa_lineas.updateExtents()
        return capa_lineas

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
        umbral_longitud_total_tanda
    ):
        UMBRAL_NUDOS_MIN = float(umbral_nudos_min)
        UMBRAL_NUDOS = float(umbral_nudos)
        UMBRAL_DISTANCIA_M = float(umbral_distancia)
        UMBRAL_MAXIMO_TANDA_M = float(umbral_max_tanda)
        MIN_LINEAS_TANDA = int(min_lineas_tanda)
        UMBRAL_CAMBIO_ANGULO = float(umbral_cambio_angulo)
        UMBRAL_LONGITUD_TOTAL_TANDA_M = float(umbral_longitud_total_tanda)

        # Segmentos válidos mínimos a cada lado para rellenar huecos
        MIN_VALIDOS_POR_LADO = 3

        # Número máximo de segmentos no válidos seguidos que se permiten rellenar
        MAX_HUECO = 3

        if not capa_lineas.isEditable():
            capa_lineas.startEditing()

        idx_barco = capa_lineas.fields().indexFromName("barco")
        idx_jornada = capa_lineas.fields().indexFromName("jornada")
        idx_fecha_ini = capa_lineas.fields().indexFromName("fecha_ini")
        idx_long_m = capa_lineas.fields().indexFromName("long_m")
        idx_vel_nudos = capa_lineas.fields().indexFromName("vel_nudos")
        idx_angulo = capa_lineas.fields().indexFromName("angulo")
        idx_delta_ant = capa_lineas.fields().indexFromName("delta_ant")
        idx_delta_sig = capa_lineas.fields().indexFromName("delta_sig")
        idx_es_valida = capa_lineas.fields().indexFromName("es_valida")
        idx_tanda = capa_lineas.fields().indexFromName("tanda")
        idx_cod_tanda = capa_lineas.fields().indexFromName("cod_tanda")

        lineas = []

        for feat in capa_lineas.getFeatures():
            dt = self.convertir_a_qdatetime(feat[idx_fecha_ini])

            if dt is None or not dt.isValid():
                continue

            lineas.append({
                "fid": feat.id(),
                "barco": str(feat[idx_barco]).strip(),
                "jornada": str(feat[idx_jornada]).strip(),
                "fecha_ini": dt,
                "long_m": self.valor_float_seguro(feat[idx_long_m]),
                "vel_nudos": self.valor_float_seguro(feat[idx_vel_nudos]),
                "angulo": self.valor_float_seguro(feat[idx_angulo]),
                "delta_ant": None,
                "delta_sig": None,
                "es_valida": False,
                "tanda": None,
                "cod_tanda": None
            })

        lineas.sort(
            key=lambda x: (
                x["barco"],
                x["jornada"],
                x["fecha_ini"].toSecsSinceEpoch()
            )
        )
        # ======================================================
        # UMBRAL DINÁMICO DE VELOCIDAD
        # Se activa solo para los artes que tengan:
        # "umbral_dinamico": true en el JSON
        # ======================================================

        UMBRAL_NUDOS_DINAMICO = UMBRAL_NUDOS

        usar_umbral_dinamico = False

        if arte_txt in self.parametros_artes:
            usar_umbral_dinamico = bool(
                self.parametros_artes[arte_txt].get("umbral_dinamico", False)
            )

        if usar_umbral_dinamico:
            vels = [
                lin["vel_nudos"]
                for lin in lineas
                if lin["vel_nudos"] > 0
            ]

            if vels:
                vel_media = sum(vels) / len(vels)

                UMBRAL_NUDOS_DINAMICO = max(
                    UMBRAL_NUDOS,
                    vel_media * 1.3
                )

            self.log(f"Umbral dinámico activado para {arte_txt}: {round(UMBRAL_NUDOS_DINAMICO, 2)} nudos")
        else:
            self.log(f"Umbral fijo para {arte_txt}: {round(UMBRAL_NUDOS, 2)} nudos")

        # ======================================================
        # 1. Marcar segmentos válidos según velocidad, distancia,
        # longitud máxima y cambio angular
        # ======================================================

        for i in range(len(lineas)):
            lin = lineas[i]
            grupo = (lin["barco"], lin["jornada"])

            delta_ant = None
            delta_sig = None

            if i > 0:
                ant = lineas[i - 1]
                if (ant["barco"], ant["jornada"]) == grupo:
                    delta_ant = self.diferencia_angular(lin["angulo"], ant["angulo"])

            if i < len(lineas) - 1:
                sig = lineas[i + 1]
                if (sig["barco"], sig["jornada"]) == grupo:
                    delta_sig = self.diferencia_angular(lin["angulo"], sig["angulo"])

            lin["delta_ant"] = delta_ant
            lin["delta_sig"] = delta_sig

            cambio_brusco = False

            if delta_ant is not None and delta_ant > UMBRAL_CAMBIO_ANGULO:
                cambio_brusco = True

            if delta_sig is not None and delta_sig > UMBRAL_CAMBIO_ANGULO:
                cambio_brusco = True

            es_valida = (
                lin["long_m"] <= UMBRAL_MAXIMO_TANDA_M and
                (
                    (
                        lin["vel_nudos"] <= UMBRAL_NUDOS_DINAMICO and
                        lin["vel_nudos"] >= UMBRAL_NUDOS_MIN
                    )
                    or
                    lin["long_m"] <= UMBRAL_DISTANCIA_M
                )
            )

            if cambio_brusco:
                es_valida = False

            lin["es_valida"] = es_valida
        
        
        # ======================================================
        # 2. Rellenar huecos pequeños dentro de una posible tanda
        # Ejemplo:
        # válido válido válido - no válido - válido válido válido
        # válido válido válido - no válido no válido - válido válido válido
        #
        # Esto NO cambia el mínimo de segmentos para formar tanda.
        # Solo evita que pequeños huecos rompan una tanda larga.
        # ======================================================

        i = 0

        while i < len(lineas):
            lin = lineas[i]

            if lin["es_valida"]:
                i += 1
                continue

            grupo = (lin["barco"], lin["jornada"])

            inicio_hueco = i
            fin_hueco = i

            while (
                fin_hueco + 1 < len(lineas) and
                not lineas[fin_hueco + 1]["es_valida"] and
                (lineas[fin_hueco + 1]["barco"], lineas[fin_hueco + 1]["jornada"]) == grupo
            ):
                fin_hueco += 1

            tam_hueco = fin_hueco - inicio_hueco + 1

            validas_antes = 0
            j = inicio_hueco - 1

            while j >= 0:
                ant = lineas[j]

                if (ant["barco"], ant["jornada"]) != grupo:
                    break

                if not ant["es_valida"]:
                    break

                validas_antes += 1
                j -= 1

            validas_despues = 0
            j = fin_hueco + 1

            while j < len(lineas):
                sig = lineas[j]

                if (sig["barco"], sig["jornada"]) != grupo:
                    break

                if not sig["es_valida"]:
                    break

                validas_despues += 1
                j += 1

            if (
                tam_hueco <= MAX_HUECO and
                validas_antes >= MIN_VALIDOS_POR_LADO and
                validas_despues >= MIN_VALIDOS_POR_LADO
            ):
                for k in range(inicio_hueco, fin_hueco + 1):
                    lineas[k]["es_valida"] = True

            i = fin_hueco + 1

        # ======================================================
        # 3. Crear tandas: aquí sí se exige el mínimo de segmentos
        # consecutivos que pusiste en la ventana, por ejemplo 60
        # ======================================================

        def cerrar_racha(lista_lineas, inicio, longitud, tanda_num, jornada_val):
            if inicio is None or longitud < MIN_LINEAS_TANDA:
                return tanda_num

            longitud_total = 0.0

            for j in range(inicio, inicio + longitud):
                longitud_total += self.valor_float_seguro(lista_lineas[j]["long_m"])

            if longitud_total > UMBRAL_LONGITUD_TOTAL_TANDA_M:
                return tanda_num

            tanda_num += 1

            for j in range(inicio, inicio + longitud):
                lista_lineas[j]["tanda"] = tanda_num
                lista_lineas[j]["cod_tanda"] = f"{jornada_val}00{tanda_num:02d}"

            return tanda_num

        grupo_actual = None
        jornada_actual = None
        tanda_actual = 0
        racha_inicio = None
        racha_longitud = 0

        for i, lin in enumerate(lineas):
            grupo = (lin["barco"], lin["jornada"])

            if grupo != grupo_actual:
                if grupo_actual is not None:
                    tanda_actual = cerrar_racha(
                        lineas,
                        racha_inicio,
                        racha_longitud,
                        tanda_actual,
                        jornada_actual
                    )

                grupo_actual = grupo
                jornada_actual = lin["jornada"]
                tanda_actual = 0
                racha_inicio = None
                racha_longitud = 0

            if lin["es_valida"]:
                if racha_inicio is None:
                    racha_inicio = i
                    racha_longitud = 1
                else:
                    racha_longitud += 1
            else:
                tanda_actual = cerrar_racha(
                    lineas,
                    racha_inicio,
                    racha_longitud,
                    tanda_actual,
                    jornada_actual
                )

                racha_inicio = None
                racha_longitud = 0

        if lineas:
            cerrar_racha(
                lineas,
                racha_inicio,
                racha_longitud,
                tanda_actual,
                jornada_actual
            )

        cambios = {}

        for lin in lineas:
            cambios[lin["fid"]] = {
                idx_delta_ant: lin["delta_ant"],
                idx_delta_sig: lin["delta_sig"],
                idx_es_valida: 1 if lin["es_valida"] else 0,
                idx_tanda: lin["tanda"],
                idx_cod_tanda: lin["cod_tanda"]
            }

        if cambios:
            capa_lineas.dataProvider().changeAttributeValues(cambios)

        capa_lineas.commitChanges()
        return capa_lineas

    def unir_tandas(self, capa_lineas):
        capa_salida = self.crear_capa_tandas_salida(
            capa_lineas.crs().authid(),
            "tandas_tmp"
        )

        prov = capa_salida.dataProvider()

        idx_barco = capa_lineas.fields().indexFromName("barco")
        idx_jornada = capa_lineas.fields().indexFromName("jornada")
        idx_cod_tanda = capa_lineas.fields().indexFromName("cod_tanda")
        idx_tanda = capa_lineas.fields().indexFromName("tanda")
        idx_fecha_ini = capa_lineas.fields().indexFromName("fecha_ini")
        idx_fecha_fin = capa_lineas.fields().indexFromName("fecha_fin")
        idx_tiempo_min = capa_lineas.fields().indexFromName("tiempo_min")
        idx_tiempo_h = capa_lineas.fields().indexFromName("tiempo_h")
        idx_long_m = capa_lineas.fields().indexFromName("long_m")

        grupos = {}

        for feat in capa_lineas.getFeatures():
            cod_tanda = feat[idx_cod_tanda]
            tanda = feat[idx_tanda]

            if cod_tanda is None or str(cod_tanda).strip() == "":
                continue

            if tanda is None or str(tanda).strip() in ("", "NULL", "QVariant()"):
                continue

            barco = str(feat[idx_barco]).strip()
            jornada = str(feat[idx_jornada]).strip()
            cod_tanda = str(cod_tanda).strip()

            clave = (barco, jornada, cod_tanda)

            if clave not in grupos:
                grupos[clave] = []

            grupos[clave].append({
                "geom": QgsGeometry(feat.geometry()),
                "barco": barco,
                "jornada": jornada,
                "cod_tanda": cod_tanda,
                "tanda": self.valor_float_seguro(feat[idx_tanda]),
                "fecha_ini": self.convertir_a_qdatetime(feat[idx_fecha_ini]),
                "fecha_fin": self.convertir_a_qdatetime(feat[idx_fecha_fin]),
                "tiempo_min": self.valor_float_seguro(feat[idx_tiempo_min]),
                "tiempo_h": self.valor_float_seguro(feat[idx_tiempo_h]),
                "long_m": self.valor_float_seguro(feat[idx_long_m]),
            })

        nuevas = []

        for clave, lista in grupos.items():
            barco, jornada, cod_tanda = clave

            lista.sort(
                key=lambda x: x["fecha_ini"].toSecsSinceEpoch() if x["fecha_ini"] is not None else 0
            )

            geoms = []
            long_total = 0.0
            tiempo_total_min = 0.0
            tiempo_total_h = 0.0
            fecha_ini_tanda = None
            fecha_fin_tanda = None

            for item in lista:
                g = item["geom"]

                if g is not None and not g.isEmpty():
                    geoms.append(g)
                    long_total += item["long_m"]
                    tiempo_total_min += item["tiempo_min"]
                    tiempo_total_h += item["tiempo_h"]

                if item["fecha_ini"] is not None:
                    if fecha_ini_tanda is None or item["fecha_ini"] < fecha_ini_tanda:
                        fecha_ini_tanda = item["fecha_ini"]

                if item["fecha_fin"] is not None:
                    if fecha_fin_tanda is None or item["fecha_fin"] > fecha_fin_tanda:
                        fecha_fin_tanda = item["fecha_fin"]

            if not geoms:
                continue

            geom_union = QgsGeometry.unaryUnion(geoms)

            if geom_union is None or geom_union.isEmpty():
                continue

            geom_primera = lista[0]["geom"]
            geom_ultima = lista[-1]["geom"]

            pts_ini = geom_primera.asPolyline()
            pts_fin = geom_ultima.asPolyline()

            x_ini = pts_ini[0].x() if pts_ini else None
            y_ini = pts_ini[0].y() if pts_ini else None
            x_fin = pts_fin[-1].x() if pts_fin else None
            y_fin = pts_fin[-1].y() if pts_fin else None

            if tiempo_total_h > 0:
                vel_media_kmh = (long_total / 1000.0) / tiempo_total_h
                vel_media_nudos = vel_media_kmh / 1.852
            else:
                vel_media_kmh = None
                vel_media_nudos = None

            nueva = QgsFeature(capa_salida.fields())
            nueva.setGeometry(geom_union)
            nueva.setAttributes([
                barco,
                jornada,
                cod_tanda,
                int(self.valor_float_seguro(lista[0]["tanda"])),
                fecha_ini_tanda.toString("yyyy-MM-dd HH:mm:ss") if fecha_ini_tanda is not None else None,
                fecha_fin_tanda.toString("yyyy-MM-dd HH:mm:ss") if fecha_fin_tanda is not None else None,
                x_ini,
                y_ini,
                x_fin,
                y_fin,
                tiempo_total_min,
                tiempo_total_h,
                long_total,
                vel_media_kmh,
                vel_media_nudos,
                len(lista)
            ])

            nuevas.append(nueva)

        if nuevas:
            prov.addFeatures(nuevas)

        capa_salida.updateExtents()
        return capa_salida

    # ==========================================================
    # EXPORTACIÓN
    # ==========================================================

    def exportar_capa_a_gpkg(self, capa, ruta_gpkg, nombre_capa_gpkg, sobrescribir_archivo=False):
        if capa is None or not capa.isValid():
            self.log(f"Error: la capa '{nombre_capa_gpkg}' no es válida.")
            return False

        opciones = QgsVectorFileWriter.SaveVectorOptions()
        opciones.driverName = "GPKG"
        opciones.layerName = nombre_capa_gpkg
        opciones.fileEncoding = "UTF-8"

        if sobrescribir_archivo:
            opciones.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteFile
        else:
            opciones.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer

        res = QgsVectorFileWriter.writeAsVectorFormatV3(
            capa,
            ruta_gpkg,
            QgsProject.instance().transformContext(),
            opciones
        )

        resultado = res[0] if isinstance(res, tuple) else res
        mensaje = res[1] if isinstance(res, tuple) and len(res) > 1 else ""

        if resultado == QgsVectorFileWriter.NoError:
            self.log(f"Capa exportada correctamente: {nombre_capa_gpkg}")
            return True

        self.log(f"Error al exportar la capa: {nombre_capa_gpkg}")
        self.log(f"Código: {resultado}")
        self.log(f"Mensaje: {mensaje}")
        return False

    def exportar_resultados(
        self,
        ruta_salida,
        capa_puntos,
        capa_lineas_jornada,
        capa_lineas_segmentos,
        capa_tandas,
        nombre_base_salida,
        rango_anios
    ):
        if not ruta_salida:
            self.log("No se indicó carpeta de salida.")
            return None

        if not os.path.isdir(ruta_salida):
            self.log("La carpeta de salida no existe.")
            return None

        nombre_puntos = f"puntos_{nombre_base_salida}"
        nombre_lineas_jornada = f"lineas_jornada_{nombre_base_salida}"
        nombre_lineas = f"lineas_segmentos_{nombre_base_salida}"
        nombre_tandas = f"tandas_{nombre_base_salida}"

        nombre_base = f"resultados_{nombre_base_salida}"
        ruta_gpkg = os.path.join(ruta_salida, f"{nombre_base}.gpkg")

        contador = 1
        while os.path.exists(ruta_gpkg):
            ruta_gpkg = os.path.join(ruta_salida, f"{nombre_base}_{contador}.gpkg")
            contador += 1

        self.log("=== EXPORTACIÓN AUTOMÁTICA ===")
        self.log(f"Archivo de salida: {ruta_gpkg}")

        ok1 = self.exportar_capa_a_gpkg(capa_puntos, ruta_gpkg, nombre_puntos, True)
        ok2 = self.exportar_capa_a_gpkg(capa_lineas_jornada, ruta_gpkg, nombre_lineas_jornada, False)
        ok3 = self.exportar_capa_a_gpkg(capa_lineas_segmentos, ruta_gpkg, nombre_lineas, False)
        ok4 = self.exportar_capa_a_gpkg(capa_tandas, ruta_gpkg, nombre_tandas, False)

        if ok1 and ok2 and ok3 and ok4:
            self.log("Exportación completada correctamente.")
            return ruta_gpkg

        self.log("La exportación terminó con errores.")
        return None

    def quitar_capa_del_proyecto(self, capa):
        if capa is None:
            return

        try:
            QgsProject.instance().removeMapLayer(capa.id())
        except Exception as e:
            self.log(f"No se pudo quitar una capa temporal: {e}")

    def cargar_capa_desde_gpkg(self, ruta_gpkg, nombre_capa):
        uri = f"{ruta_gpkg}|layername={nombre_capa}"
        capa = QgsVectorLayer(uri, nombre_capa, "ogr")

        if not capa.isValid():
            self.log(f"No se pudo cargar la capa exportada: {nombre_capa}")
            return None

        QgsProject.instance().addMapLayer(capa)
        self.log(f"Capa cargada desde GeoPackage: {nombre_capa}")
        return capa

    # ==========================================================
    # PROCESO PRINCIPAL
    # ==========================================================

    def ejecutar_proceso(self):
        self.dlg.txtLog.clear()
        self.dlg.progressBar.setValue(0)
        self.dlg.progressBar.setFormat("Preparando datos...")
        QCoreApplication.processEvents()

        ruta_entrada = self.dlg.txtRutaCsv.text().strip()
        arte = self.dlg.cmbArtePesca.currentText()
        campo_barco = self.dlg.cmbCampoBarco.currentText()
        campo_fecha = self.dlg.cmbCampoFecha.currentText()
        ruta_salida = self.dlg.txtRutaSalida.text().strip()

        if hasattr(self.dlg, "spnVelMinNudos"):
            umbral_nudos_min = self.dlg.spnVelMinNudos.value()
        else:
            umbral_nudos_min = 0.0

        umbral_nudos = self.dlg.spnUmbralNudos.value()
        umbral_distancia = self.dlg.spnUmbralDistancia.value()
        umbral_max_tanda = self.dlg.spnUmbralMaxTanda.value()
        min_lineas_tanda = self.dlg.spnMinLineasTanda.value()
        umbral_cambio_angulo = self.dlg.spnUmbralAngulo.value()
        umbral_longitud_total_tanda = self.dlg.spnLongitudMaxTanda.value()

        self.log("======================================")
        self.log("        GEO PESCA - EJECUCIÓN")
        self.log("======================================")
        self.log(f"Entrada: {ruta_entrada}")
        self.log(f"Arte seleccionado: {arte}")
        self.log(f"Campo barco: {campo_barco}")
        self.log(f"Campo fecha: {campo_fecha}")
        self.log(f"Salida: {ruta_salida}")
        self.log("----------- PARÁMETROS USADOS -----------")
        self.log(f"Velocidad mínima (nudos): {umbral_nudos_min}")
        self.log(f"Velocidad máxima (nudos): {umbral_nudos}")
        self.log(f"Distancia/longitud corta válida (m): {umbral_distancia}")
        self.log(f"Longitud máxima segmento (m): {umbral_max_tanda}")
        self.log(f"Mínimo segmentos para formar tanda: {min_lineas_tanda}")
        self.log(f"Cambio angular máximo (°): {umbral_cambio_angulo}")
        self.log(f"Longitud máxima total tanda (m): {umbral_longitud_total_tanda}")
        self.log("-----------------------------------------")

        capa_entrada = self.obtener_capa_entrada(ruta_entrada)

        if capa_entrada is None:
            self.log("Error cargando capa.")
            self.dlg.tabWidget.setCurrentIndex(2)
            return

        rango_anios = self.obtener_rango_anios_datos(capa_entrada, campo_fecha)
        arte_txt = self.normalizar_nombre(arte)

        nombre_usuario = self.dlg.txtNombreCapa.text().strip()

        if nombre_usuario:
            nombre_base_salida = self.normalizar_nombre(nombre_usuario)
        else:
            nombre_base_salida = f"{arte_txt}_{rango_anios}"

        nombre_puntos = f"puntos_{nombre_base_salida}"
        nombre_lineas_jornada = f"lineas_jornada_{nombre_base_salida}"
        nombre_lineas = f"lineas_segmentos_{nombre_base_salida}"
        nombre_tandas = f"tandas_{nombre_base_salida}"

        crs_authid = capa_entrada.crs().authid()

        capa_puntos_total = self.crear_capa_puntos_salida(capa_entrada, nombre_puntos)
        capa_lineas_jornada_total = self.crear_capa_lineas_jornada_salida(crs_authid, nombre_lineas_jornada)
        capa_lineas_total = self.crear_capa_lineas_segmentos_salida(crs_authid, nombre_lineas)
        capa_tandas_total = self.crear_capa_tandas_salida(crs_authid, nombre_tandas)

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
        QCoreApplication.processEvents()

        num_puntos = 0
        num_lineas_jornada = 0
        num_segmentos = 0
        num_tandas = 0

        for i, barco in enumerate(barcos, 1):
            self.dlg.progressBar.setValue(i)
            self.dlg.progressBar.setFormat(f"Procesando barco {i}/{total_barcos}")
            QCoreApplication.processEvents()
            if i == 1 or i == len(barcos) or i % 5 == 0:
                self.log(f"[{i}/{len(barcos)}] Procesando barco: {barco}")

            request = self.crear_request_barco(campo_barco, barco)
            features_barco = []

            for feat in capa_entrada.getFeatures(request):
                dt = self.convertir_a_qdatetime(feat[campo_fecha])

                if dt is None or not dt.isValid():
                    self.log(f"Fecha no válida en barco {barco}: {feat[campo_fecha]}")
                    continue


                barco_val = str(feat[campo_barco]).strip()

                if not barco_val:
                    continue

                jornada = f"{barco_val}_{dt.date().toString('yyyyMMdd')}"

                nueva = QgsFeature(capa_puntos_total.fields())
                nueva.setGeometry(QgsGeometry(feat.geometry()))

                attrs = []
                for campo in capa_puntos_total.fields():
                    nombre = campo.name()

                    if nombre == "jornada_":
                        attrs.append(jornada)
                    elif feat.fields().indexFromName(nombre) != -1:
                        attrs.append(feat[nombre])
                    else:
                        attrs.append(None)

                nueva.setAttributes(attrs)
                features_barco.append(nueva)

            if not features_barco:
                self.log(f"Barco {barco}: 0 puntos válidos")
                continue
            capa_puntos_total.dataProvider().addFeatures(features_barco)
            num_puntos += len(features_barco)

            capa_temp = QgsVectorLayer(
                f"Point?crs={crs_authid}",
                "tmp",
                "memory"
            )

            prov_temp = capa_temp.dataProvider()
            prov_temp.addAttributes(capa_puntos_total.fields())
            capa_temp.updateFields()
            prov_temp.addFeatures(features_barco)
            capa_temp.updateExtents()

            capa_lineas_jornada_sub = self.crear_lineas_jornada(
                capa_temp,
                campo_barco,
                campo_fecha
            )

            if capa_lineas_jornada_sub.featureCount() > 0:
                num_lineas_jornada += capa_lineas_jornada_sub.featureCount()
                self.copiar_features(capa_lineas_jornada_sub, capa_lineas_jornada_total)

            capa_lineas_sub = self.crear_lineas_segmentos(
                capa_temp,
                campo_barco,
                campo_fecha
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
                umbral_longitud_total_tanda
            )

            self.copiar_features(capa_lineas_sub, capa_lineas_total)

            capa_tandas_sub = self.unir_tandas(capa_lineas_sub)

            if capa_tandas_sub.featureCount() > 0:
                num_tandas += capa_tandas_sub.featureCount()
                self.copiar_features(capa_tandas_sub, capa_tandas_total)

        capa_puntos_total.updateExtents()
        capa_lineas_jornada_total.updateExtents()
        capa_lineas_total.updateExtents()
        capa_tandas_total.updateExtents()


        ruta_gpkg = self.exportar_resultados(
            ruta_salida,
            capa_puntos_total,
            capa_lineas_jornada_total,
            capa_lineas_total,
            capa_tandas_total,
            nombre_base_salida,
            rango_anios
        )

        if ruta_gpkg:

            self.cargar_capa_desde_gpkg(ruta_gpkg, nombre_puntos)
            self.cargar_capa_desde_gpkg(ruta_gpkg, nombre_lineas_jornada)
            self.cargar_capa_desde_gpkg(ruta_gpkg, nombre_lineas)
            self.cargar_capa_desde_gpkg(ruta_gpkg, nombre_tandas)

        self.log("----------- RESULTADOS -----------")
        self.log(f"Puntos: {num_puntos}")
        self.log(f"Líneas por jornada: {num_lineas_jornada}")
        self.log(f"Segmentos: {num_segmentos}")
        self.log(f"Tandas: {num_tandas}")
        self.dlg.progressBar.setValue(total_barcos)
        self.dlg.progressBar.setFormat("Proceso completado")
        QCoreApplication.processEvents()
        self.log("----------------------------------")
        self.log("Proceso terminado.")
        self.dlg.tabWidget.setCurrentIndex(2)

    # ==========================================================
    # RUN
    # ==========================================================

    def run(self):
        if self.first_start is True:
            self.first_start = False
            self.dlg = geopescaDialog()

            self.dlg.btnBuscarCsv.clicked.connect(self.seleccionar_entrada)
            self.dlg.btnBuscarSalida.clicked.connect(self.seleccionar_salida)
            self.dlg.cmbArtePesca.currentIndexChanged.connect(self.aplicar_parametros_arte)
            self.dlg.button_box.button(QDialogButtonBox.Ok).clicked.connect(self.ejecutar_proceso)

            self.dlg.button_box.button(QDialogButtonBox.Ok).setText("Ejecutar")
            self.dlg.button_box.button(QDialogButtonBox.Cancel).setText("Cerrar")

        self.dlg.progressBar.setValue(0)

        self.dlg.tabWidget.setCurrentIndex(0)
        self.dlg.txtLog.clear()

        self.cargar_parametros_artes()
        self.aplicar_parametros_arte()

        self.dlg.show()
        self.dlg.exec_()
