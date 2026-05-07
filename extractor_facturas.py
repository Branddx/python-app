#!/usr/bin/env python3
"""
Extractor de Facturas Digitales en PDF → JSON
==============================================
Ajustado al formato de factura con estructura:
  - Encabezado: FECHA | N.º DE FACTURA | SU EMPRESA
  - Sección FACTURAR A (cliente)
  - Tabla de ítems: CANTIDAD | DESCRIPCIÓN | PRECIO POR UNIDAD | TOTAL DE LÍNEA
  - Totales: Subtotal / Impuesto sobre las ventas / Total

Instalación:
    pip install pdfplumber

Uso:
    python extractor_facturas.py factura.pdf
    python extractor_facturas.py carpeta/
    python extractor_facturas.py factura.pdf --salida resultado.json
    python extractor_facturas.py factura.pdf --mostrar
"""

import re
import json
import sys
import argparse
from pathlib import Path
from datetime import datetime
from typing import Optional

try:
    import pdfplumber
except ImportError:
    print("ERROR: Instala la dependencia con:  pip install pdfplumber")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# UTILIDADES
# ─────────────────────────────────────────────────────────────────────────────

def buscar(patron: str, texto: str, flags=re.IGNORECASE) -> Optional[str]:
    """Devuelve el primer grupo capturado o None."""
    m = re.search(patron, texto, flags)
    return m.group(1).strip() if m else None


def a_float(texto: Optional[str]) -> Optional[float]:
    """Convierte texto de monto a float (maneja '.' y ',' como separadores)."""
    if not texto:
        return None
    limpio = re.sub(r"[^\d,\.]", "", texto.strip())
    if "," in limpio and "." in limpio:
        limpio = limpio.replace(",", "")       # 1,234.56
    elif "," in limpio:
        limpio = limpio.replace(",", ".")      # 1234,56
    try:
        return float(limpio)
    except ValueError:
        return None


def normalizar_fecha(texto: Optional[str]) -> Optional[str]:
    """Convierte fecha al formato ISO 8601 (YYYY-MM-DD)."""
    if not texto:
        return None
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
                "%d/%m/%y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(texto.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return texto


def limpiar_precio(texto: Optional[str]) -> Optional[float]:
    """Elimina simbolos de moneda y convierte a float. Ej: '2 euro' → 2.0"""
    if not texto:
        return None
    sin_simbolo = re.sub(r"[€$£¥\s]", "", texto)
    return a_float(sin_simbolo)


# ─────────────────────────────────────────────────────────────────────────────
# EXTRACTOR PRINCIPAL
# ─────────────────────────────────────────────────────────────────────────────

class ExtractorFactura:
    """
    Extrae todos los campos relevantes de una factura en PDF
    y los devuelve como diccionario listo para serializar a JSON.
    """

    def __init__(self, ruta: str):
        self.ruta = Path(ruta)
        self.texto = ""
        self.lineas: list[str] = []

    # ── Lectura ───────────────────────────────────────────────────────────────

    def _leer(self) -> bool:
        try:
            with pdfplumber.open(self.ruta) as pdf:
                partes = []
                for page in pdf.pages:
                    t = page.extract_text()
                    if t:
                        partes.append(t)
                self.texto = "\n".join(partes)
                self.lineas = self.texto.splitlines()
            return bool(self.texto.strip())
        except Exception as e:
            print(f"  [ERROR] {self.ruta.name}: {e}")
            return False

    def _contar_paginas(self) -> int:
        try:
            with pdfplumber.open(self.ruta) as pdf:
                return len(pdf.pages)
        except Exception:
            return 0

    # ── Encabezado ────────────────────────────────────────────────────────────

    def _extraer_encabezado(self) -> dict:
        fecha = buscar(r"(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4})", self.texto)

        # El layout del PDF puede colocar fecha y número en la misma línea:
        #   "20/06/2026 10879645"
        # Buscamos esa línea y extraemos el número (lo que NO es la fecha)
        numero = buscar(
            r"\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}\s+(\d{6,12})\b",
            self.texto
        )
        if not numero:
            # Alternativa: etiqueta seguida del número en la misma línea
            numero = buscar(
                r"N\.?\s*[oOº°]\s*DE\s*FACTURA\s+([A-Z0-9\-]+)",
                self.texto, re.IGNORECASE
            )
        if not numero:
            # Fallback: número de 6-12 dígitos que aparezca solo en su línea
            for linea in self.lineas:
                m = re.fullmatch(r"\s*(\d{6,12})\s*", linea)
                if m:
                    numero = m.group(1)
                    break

        return {
            "fecha_emision":  normalizar_fecha(fecha),
            "numero_factura": numero,
        }

    # ── Emisor ────────────────────────────────────────────────────────────────

    def _extraer_bloque(self, etiqueta: str, siguiente: str) -> list[str]:
        """
        Extrae las líneas entre 'etiqueta' y 'siguiente' en el texto.
        Devuelve lista de líneas no vacías.
        """
        patron = rf"{re.escape(etiqueta)}\s*\n([\s\S]+?)(?=\n{re.escape(siguiente)}|\Z)"
        bloque = buscar(patron, self.texto, re.IGNORECASE)
        if not bloque:
            return []
        return [l.strip() for l in bloque.splitlines() if l.strip()]

    def _parsear_contacto(self, lineas: list[str]) -> dict:
        """Convierte una lista de líneas de contacto en campos estructurados."""
        direccion = ciudad = telefono = email = None
        for l in lineas:
            # Saltar líneas que mezclan fecha y número (artefacto del PDF layout)
            if re.search(r"\d{1,2}/\d{2}/\d{4}", l) and re.search(r"\b\d{6,}\b", l):
                continue
            if re.fullmatch(r"[\d\s\-\+\(\)]{6,15}", l):
                telefono = l
            elif re.search(r"[\w.\-]+@[\w.\-]+\.\w{2,}", l):
                email = re.search(r"[\w.\-]+@[\w.\-]+\.\w{2,}", l).group()
            elif re.match(r"Av\.|Jr\.|Calle|Pasaje|Urb\.|Mz\.|Lt\.", l, re.IGNORECASE):
                if not direccion:
                    direccion = l
            elif not direccion:
                direccion = l
            elif not ciudad:
                ciudad = l
        return {"direccion": direccion, "ciudad": ciudad,
                "telefono": telefono, "email": email}

    def _extraer_emisor(self) -> dict:
        lineas = self._extraer_bloque("SU EMPRESA", "FACTURAR A")
        # fallback: buscar desde "SU EMPRESA" hasta la tabla
        if not lineas:
            lineas = self._extraer_bloque("SU EMPRESA", "CANTIDAD")
        campos = self._parsear_contacto(lineas)
        campos["nombre"] = "SU EMPRESA"
        return campos

    def _extraer_cliente(self) -> dict:
        lineas = self._extraer_bloque("FACTURAR A", "CANTIDAD")
        return self._parsear_contacto(lineas)

    # ── Ítems ─────────────────────────────────────────────────────────────────

    def _extraer_items(self) -> list[dict]:
        """
        Detecta filas de la tabla de productos con el patrón:
          <cantidad>  <descripcion>  <precio_unitario €>  <total_linea €>
        """
        patron_fila = re.compile(
            r"^(\d+(?:[,\.]\d+)?)\s+"       # cantidad (entero o decimal)
            r"(.+?)\s+"                      # descripción (cualquier texto)
            r"([\d,\.]+\s*[€$£¥]?)\s+"      # precio por unidad
            r"([\d,\.]+\s*[€$£¥]?)$",       # total de línea
            re.MULTILINE
        )

        items = []
        for m in patron_fila.finditer(self.texto):
            desc = m.group(2).strip()
            # Omitir si parece encabezado de columna
            if re.search(
                r"DESCRIPCI[ÓO]N|PRECIO|CANTIDAD|TOTAL|UNIDAD",
                desc, re.IGNORECASE
            ):
                continue
            items.append({
                "cantidad":        a_float(m.group(1)),
                "descripcion":     desc,
                "precio_unitario": limpiar_precio(m.group(3)),
                "total_linea":     limpiar_precio(m.group(4)),
            })

        return items

    # ── Totales ───────────────────────────────────────────────────────────────

    def _extraer_totales(self) -> dict:
        subtotal = buscar(r"Subtotal\s+([\d,\.]+)", self.texto)
        impuesto = buscar(
            r"Impuesto(?:\s+sobre\s+las\s+ventas)?\s+([\d,\.]+)", self.texto
        )
        total    = buscar(r"\bTotal\b\s+([\d,\.]+)", self.texto)
        moneda_m = re.search(r"[€$£¥]|\bEUR\b|\bUSD\b|\bPEN\b|\bMXN\b", self.texto)

        return {
            "moneda":                moneda_m.group() if moneda_m else None,
            "subtotal":              a_float(subtotal),
            "impuesto_sobre_ventas": a_float(impuesto),
            "total":                 a_float(total),
        }

    # ── Punto de entrada ──────────────────────────────────────────────────────

    def extraer(self) -> dict:
        if not self._leer():
            return {
                "error":   "No se pudo extraer texto del PDF",
                "archivo": str(self.ruta),
            }

        return {
            "metadata": {
                "archivo":      self.ruta.name,
                "procesado_en": datetime.now().isoformat(timespec="seconds"),
                "paginas":      self._contar_paginas(),
            },
            "comprobante": self._extraer_encabezado(),
            "emisor":      self._extraer_emisor(),
            "cliente":     self._extraer_cliente(),
            "items":       self._extraer_items(),
            "totales":     self._extraer_totales(),
        }


# ─────────────────────────────────────────────────────────────────────────────
# PROCESAMIENTO EN LOTE
# ─────────────────────────────────────────────────────────────────────────────

def procesar_uno(ruta: str) -> dict:
    print(f"  -> {Path(ruta).name}")
    return ExtractorFactura(ruta).extraer()


def procesar_directorio(directorio: str) -> list[dict]:
    pdfs = sorted(Path(directorio).glob("*.pdf"))
    if not pdfs:
        print(f"No se encontraron PDFs en: {directorio}")
        return []
    print(f"Encontrados {len(pdfs)} PDF(s):")
    return [procesar_uno(str(p)) for p in pdfs]


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Extrae datos de facturas PDF y los exporta a JSON."
    )
    parser.add_argument("entrada",
        help="Ruta al PDF o a un directorio con multiples PDFs.")
    parser.add_argument("--salida", "-o", default=None,
        help="Archivo JSON de salida. Por defecto: <entrada>.json")
    parser.add_argument("--mostrar", action="store_true",
        help="Imprime el JSON en pantalla ademas de guardarlo.")
    args = parser.parse_args()

    entrada = Path(args.entrada)

    if entrada.is_dir():
        datos          = procesar_directorio(str(entrada))
        salida_default = entrada / "facturas.json"
    elif entrada.is_file() and entrada.suffix.lower() == ".pdf":
        datos          = procesar_uno(str(entrada))
        salida_default = entrada.with_suffix(".json")
    else:
        print(f"ERROR: '{entrada}' no es un PDF ni un directorio valido.")
        sys.exit(1)

    ruta_salida = Path(args.salida) if args.salida else salida_default
    with open(ruta_salida, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)

    if args.mostrar:
        print(json.dumps(datos, ensure_ascii=False, indent=2))

    print(f"\nJSON guardado en: {ruta_salida}")

    # Resumen rapido
    registros = datos if isinstance(datos, list) else [datos]
    for r in registros:
        c = r.get("comprobante", {})
        t = r.get("totales", {})
        n = len(r.get("items", []))
        print(f"  Factura N: {c.get('numero_factura','?')} | "
              f"Fecha: {c.get('fecha_emision','?')} | "
              f"Total: {t.get('total','?')} {t.get('moneda','')} | "
              f"Items: {n}")


if __name__ == "__main__":
    main()