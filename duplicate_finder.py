#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gestor de Archivos Duplicados
==============================

Aplicacion de escritorio (Windows 10 / macOS / Linux) para detectar
archivos duplicados por CONTENIDO (no solo por nombre), permitiendo:

  - Analizar una o varias carpetas (y sus subcarpetas) a la vez.
  - Detectar duplicados EXACTOS (mismo contenido, hash SHA-256 identico)
    aunque esten en carpetas distintas (ej: Documentos, Descargas, Escritorio).
  - Eliminar automaticamente la version mas antigua de cada grupo de
    duplicados exactos, conservando la mas reciente (se envia a la
    Papelera de reciclaje si el paquete 'send2trash' esta instalado).
  - Detectar archivos con el MISMO NOMBRE pero contenido DISTINTO, y
    permitir renombrarlos para evitar confusiones/ahorrar espacio de
    forma segura (nunca se borra contenido distinto automaticamente).

Requisitos:
    - Python 3.8 o superior (ya viene en muchos sistemas; en Windows se
      descarga gratis desde https://www.python.org/downloads/).
    - (Opcional pero recomendado) paquete 'send2trash' para poder enviar
      los archivos a la papelera en vez de borrarlos en forma permanente:
          pip install send2trash

Ejecucion:
    python duplicate_finder.py
"""

import os
import sys
import hashlib
import threading
import queue
import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

try:
    from send2trash import send2trash
    HAS_SEND2TRASH = True
except ImportError:
    HAS_SEND2TRASH = False


# --------------------------------------------------------------------------
# Utilidades de bajo nivel
# --------------------------------------------------------------------------

def human_size(num_bytes: int) -> str:
    """Convierte bytes a una representacion legible (KB, MB, GB...)."""
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num_bytes < step:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= step
    return f"{num_bytes:.1f} PB"


def human_date(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def compute_hash(path: str, chunk_size: int = 1024 * 1024) -> str:
    """Calcula el hash SHA-256 del contenido de un archivo."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class FileInfo:
    __slots__ = ("path", "size", "mtime", "hash")

    def __init__(self, path, size, mtime, hash_=None):
        self.path = path
        self.size = size
        self.mtime = mtime
        self.hash = hash_

    @property
    def name(self):
        return os.path.basename(self.path)

    @property
    def folder(self):
        return os.path.dirname(self.path)


# --------------------------------------------------------------------------
# Logica de escaneo (corre en un hilo aparte para no congelar la interfaz)
# --------------------------------------------------------------------------

def scan_directories(directories, include_subfolders, progress_queue, stop_event):
    """
    Recorre las carpetas indicadas y devuelve:
      - exact_groups: lista de listas de FileInfo con hash identico (>1 elem.)
      - name_groups:  lista de listas de FileInfo con mismo nombre pero
                      distinto contenido (>1 elem.)
    Envia mensajes de progreso a progress_queue.
    """
    all_files = []

    # 1) Recolectar archivos
    for base_dir in directories:
        if stop_event.is_set():
            return [], []
        if not os.path.isdir(base_dir):
            continue
        if include_subfolders:
            walker = os.walk(base_dir)
        else:
            walker = [(base_dir, [], [
                f for f in os.listdir(base_dir)
                if os.path.isfile(os.path.join(base_dir, f))
            ])]

        for root, _dirs, files in walker:
            for fname in files:
                if stop_event.is_set():
                    return [], []
                fpath = os.path.join(root, fname)
                try:
                    st = os.stat(fpath)
                except OSError:
                    continue
                all_files.append(FileInfo(fpath, st.st_size, st.st_mtime))

    progress_queue.put(("status", f"Encontrados {len(all_files)} archivos. Comparando tamanos..."))

    # 2) Agrupar por tamano primero (filtro rapido: si el tamano difiere,
    #    el contenido no puede ser identico -> ahorra calcular hash de todo)
    by_size = {}
    for fi in all_files:
        by_size.setdefault(fi.size, []).append(fi)

    candidates = [group for group in by_size.values() if len(group) > 1]
    total_to_hash = sum(len(g) for g in candidates)
    hashed = 0

    # 3) Calcular hash solo de los candidatos con tamano repetido
    by_hash = {}
    for group in candidates:
        for fi in group:
            if stop_event.is_set():
                return [], []
            try:
                fi.hash = compute_hash(fi.path)
            except (OSError, PermissionError):
                continue
            hashed += 1
            progress_queue.put(("progress", hashed, total_to_hash))
            by_hash.setdefault(fi.hash, []).append(fi)

    exact_groups = [g for g in by_hash.values() if len(g) > 1]

    # 4) Detectar mismo nombre, distinto contenido (usando TODOS los archivos)
    by_name = {}
    for fi in all_files:
        by_name.setdefault(fi.name.lower(), []).append(fi)

    name_groups = []
    for group in by_name.values():
        if len(group) < 2:
            continue
        # Si ya tienen hash (fueron candidatos por tamano) lo usamos;
        # si no, lo calculamos ahora para saber si son distintos.
        hashes = set()
        for fi in group:
            if fi.hash is None:
                try:
                    fi.hash = compute_hash(fi.path)
                except (OSError, PermissionError):
                    continue
            hashes.add(fi.hash)
        # Si hay mas de un hash distinto entre archivos con el mismo nombre,
        # es un grupo de "mismo nombre, contenido distinto"
        if len(hashes) > 1:
            name_groups.append(group)

    progress_queue.put(("done", exact_groups, name_groups))
    return exact_groups, name_groups


# --------------------------------------------------------------------------
# Interfaz grafica
# --------------------------------------------------------------------------

WELL_KNOWN_FOLDERS = {
    "Documentos": "Documents",
    "Descargas": "Downloads",
    "Escritorio": "Desktop",
    "Imagenes": "Pictures",
    "Musica": "Music",
    "Videos": "Videos",
}


class DuplicateFinderApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Gestor de Archivos Duplicados")
        self.geometry("980x680")
        self.minsize(820, 560)

        self.directories = []
        self.include_subfolders = tk.BooleanVar(value=True)
        self.progress_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.scan_thread = None

        self.exact_groups = []
        self.name_groups = []

        self._build_ui()
        self._add_default_folders()
        self.after(150, self._poll_queue)

    # ------------------------- construccion de UI -------------------------

    def _build_ui(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="Carpetas a analizar:", font=("Segoe UI", 10, "bold")).pack(anchor="w")

        dir_frame = ttk.Frame(top)
        dir_frame.pack(fill="x", pady=(4, 4))

        self.dir_listbox = tk.Listbox(dir_frame, height=5)
        self.dir_listbox.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(dir_frame, command=self.dir_listbox.yview)
        scroll.pack(side="left", fill="y")
        self.dir_listbox.config(yscrollcommand=scroll.set)

        btns = ttk.Frame(dir_frame)
        btns.pack(side="left", fill="y", padx=(8, 0))
        ttk.Button(btns, text="Agregar carpeta...", command=self._add_folder_dialog).pack(fill="x", pady=2)
        ttk.Button(btns, text="Quitar seleccionada", command=self._remove_selected_folder).pack(fill="x", pady=2)
        for label in WELL_KNOWN_FOLDERS:
            ttk.Button(btns, text=f"+ {label}", command=lambda l=label: self._add_well_known(l)).pack(fill="x", pady=1)

        options = ttk.Frame(top)
        options.pack(fill="x", pady=(4, 8))
        ttk.Checkbutton(options, text="Incluir subcarpetas", variable=self.include_subfolders).pack(side="left")

        action_frame = ttk.Frame(options)
        action_frame.pack(side="right")
        self.scan_btn = ttk.Button(action_frame, text="Escanear", command=self._start_scan)
        self.scan_btn.pack(side="left", padx=4)
        self.cancel_btn = ttk.Button(action_frame, text="Cancelar", command=self._cancel_scan, state="disabled")
        self.cancel_btn.pack(side="left", padx=4)

        self.progress_bar = ttk.Progressbar(top, mode="determinate")
        self.progress_bar.pack(fill="x", pady=(0, 4))
        self.status_label = ttk.Label(top, text="Listo.")
        self.status_label.pack(anchor="w")

        # Pestañas de resultados
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.exact_tab = ttk.Frame(self.notebook)
        self.name_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.exact_tab, text="Duplicados exactos (0)")
        self.notebook.add(self.name_tab, text="Mismo nombre, distinto contenido (0)")

        self._build_exact_tab()
        self._build_name_tab()

        if not HAS_SEND2TRASH:
            note = ttk.Label(
                self,
                text="Nota: instala el paquete 'send2trash' (pip install send2trash) para enviar "
                     "los archivos a la Papelera en lugar de borrarlos en forma permanente.",
                foreground="#8a5a00",
            )
            note.pack(fill="x", padx=10, pady=(0, 6))

    def _build_exact_tab(self):
        frame = self.exact_tab
        info = ttk.Label(
            frame,
            text="Estos grupos de archivos tienen contenido IDENTICO. "
                 "Se sugiere conservar el mas reciente y eliminar el resto.",
            wraplength=920, justify="left",
        )
        info.pack(anchor="w", padx=6, pady=6)

        cols = ("keep", "path", "size", "modified")
        self.exact_tree = ttk.Treeview(frame, columns=cols, show="tree headings", selectmode="extended")
        self.exact_tree.heading("#0", text="Grupo")
        self.exact_tree.heading("keep", text="Estado")
        self.exact_tree.heading("path", text="Ruta")
        self.exact_tree.heading("size", text="Tamano")
        self.exact_tree.heading("modified", text="Modificado")
        self.exact_tree.column("#0", width=90)
        self.exact_tree.column("keep", width=90)
        self.exact_tree.column("path", width=520)
        self.exact_tree.column("size", width=90)
        self.exact_tree.column("modified", width=150)
        self.exact_tree.pack(fill="both", expand=True, padx=6)

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x", padx=6, pady=6)
        ttk.Button(
            btn_frame, text="Eliminar automaticamente los mas antiguos de cada grupo",
            command=self._delete_oldest_all_groups,
        ).pack(side="left", padx=4)
        ttk.Button(
            btn_frame, text="Eliminar archivos seleccionados",
            command=self._delete_selected_exact,
        ).pack(side="left", padx=4)

    def _build_name_tab(self):
        frame = self.name_tab
        info = ttk.Label(
            frame,
            text="Estos archivos comparten el mismo nombre pero su contenido es DISTINTO. "
                 "No se eliminan automaticamente: podes renombrarlos para diferenciarlos.",
            wraplength=920, justify="left",
        )
        info.pack(anchor="w", padx=6, pady=6)

        cols = ("path", "size", "modified")
        self.name_tree = ttk.Treeview(frame, columns=cols, show="tree headings", selectmode="browse")
        self.name_tree.heading("#0", text="Grupo (nombre)")
        self.name_tree.heading("path", text="Ruta")
        self.name_tree.heading("size", text="Tamano")
        self.name_tree.heading("modified", text="Modificado")
        self.name_tree.column("#0", width=180)
        self.name_tree.column("path", width=520)
        self.name_tree.column("size", width=90)
        self.name_tree.column("modified", width=150)
        self.name_tree.pack(fill="both", expand=True, padx=6)

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x", padx=6, pady=6)
        ttk.Button(btn_frame, text="Renombrar seleccionado...", command=self._rename_selected).pack(side="left", padx=4)

    # ------------------------- manejo de carpetas -------------------------

    def _add_default_folders(self):
        home = os.path.expanduser("~")
        for spanish, english in (("Documentos", "Documents"), ("Descargas", "Downloads"), ("Escritorio", "Desktop")):
            candidate = os.path.join(home, english)
            if os.path.isdir(candidate) and candidate not in self.directories:
                self.directories.append(candidate)
                self.dir_listbox.insert("end", candidate)

    def _add_well_known(self, spanish_label):
        home = os.path.expanduser("~")
        english = WELL_KNOWN_FOLDERS[spanish_label]
        candidate = os.path.join(home, english)
        if os.path.isdir(candidate):
            if candidate not in self.directories:
                self.directories.append(candidate)
                self.dir_listbox.insert("end", candidate)
        else:
            messagebox.showinfo("No encontrada", f"No se encontro la carpeta '{english}' en {home}")

    def _add_folder_dialog(self):
        folder = filedialog.askdirectory(title="Selecciona una carpeta para analizar")
        if folder and folder not in self.directories:
            self.directories.append(folder)
            self.dir_listbox.insert("end", folder)

    def _remove_selected_folder(self):
        sel = list(self.dir_listbox.curselection())
        for idx in reversed(sel):
            self.dir_listbox.delete(idx)
            del self.directories[idx]

    # ------------------------- escaneo -------------------------

    def _start_scan(self):
        if not self.directories:
            messagebox.showwarning("Sin carpetas", "Agrega al menos una carpeta para analizar.")
            return
        if self.scan_thread and self.scan_thread.is_alive():
            return

        self.exact_tree.delete(*self.exact_tree.get_children())
        self.name_tree.delete(*self.name_tree.get_children())
        self.progress_bar["value"] = 0
        self.status_label.config(text="Escaneando archivos...")
        self.scan_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.stop_event.clear()

        self.scan_thread = threading.Thread(
            target=scan_directories,
            args=(list(self.directories), self.include_subfolders.get(), self.progress_queue, self.stop_event),
            daemon=True,
        )
        self.scan_thread.start()

    def _cancel_scan(self):
        self.stop_event.set()
        self.status_label.config(text="Cancelando...")

    def _poll_queue(self):
        try:
            while True:
                msg = self.progress_queue.get_nowait()
                kind = msg[0]
                if kind == "status":
                    self.status_label.config(text=msg[1])
                elif kind == "progress":
                    _, done, total = msg
                    if total:
                        self.progress_bar["maximum"] = total
                        self.progress_bar["value"] = done
                    self.status_label.config(text=f"Comparando contenido: {done}/{total} archivos")
                elif kind == "done":
                    _, exact_groups, name_groups = msg
                    self.exact_groups = exact_groups
                    self.name_groups = name_groups
                    self._populate_results()
                    self.status_label.config(
                        text=f"Escaneo completo. {len(exact_groups)} grupos de duplicados exactos, "
                             f"{len(name_groups)} grupos con mismo nombre y contenido distinto."
                    )
                    self.scan_btn.config(state="normal")
                    self.cancel_btn.config(state="disabled")
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    def _populate_results(self):
        self.exact_tree.delete(*self.exact_tree.get_children())
        for i, group in enumerate(self.exact_groups, start=1):
            group_sorted = sorted(group, key=lambda fi: fi.mtime, reverse=True)  # mas nuevo primero
            parent = self.exact_tree.insert("", "end", text=f"Grupo {i}", open=True)
            for j, fi in enumerate(group_sorted):
                estado = "Conservar (mas nuevo)" if j == 0 else "Mas antiguo"
                self.exact_tree.insert(
                    parent, "end", iid=f"{i}-{j}-{fi.path}",
                    values=(estado, fi.path, human_size(fi.size), human_date(fi.mtime)),
                    tags=("keep" if j == 0 else "old",),
                )
        self.exact_tree.tag_configure("keep", foreground="#0a6e0a")
        self.exact_tree.tag_configure("old", foreground="#a30000")

        self.name_tree.delete(*self.name_tree.get_children())
        for i, group in enumerate(self.name_groups, start=1):
            name = group[0].name
            parent = self.name_tree.insert("", "end", text=f"{name}", open=True)
            for fi in sorted(group, key=lambda f: f.mtime, reverse=True):
                self.name_tree.insert(
                    parent, "end", iid=f"n{i}-{fi.path}",
                    values=(fi.path, human_size(fi.size), human_date(fi.mtime)),
                )

        self.notebook.tab(self.exact_tab, text=f"Duplicados exactos ({len(self.exact_groups)})")
        self.notebook.tab(self.name_tab, text=f"Mismo nombre, distinto contenido ({len(self.name_groups)})")

    # ------------------------- acciones sobre duplicados exactos -------------------------

    def _delete_paths(self, paths):
        errors = []
        deleted = 0
        for path in paths:
            try:
                if HAS_SEND2TRASH:
                    send2trash(path)
                else:
                    os.remove(path)
                deleted += 1
            except Exception as e:  # noqa: BLE001
                errors.append(f"{path}: {e}")
        return deleted, errors

    def _delete_oldest_all_groups(self):
        if not self.exact_groups:
            messagebox.showinfo("Sin datos", "No hay grupos de duplicados. Ejecuta un escaneo primero.")
            return

        to_delete = []
        for group in self.exact_groups:
            group_sorted = sorted(group, key=lambda fi: fi.mtime, reverse=True)
            to_delete.extend(fi.path for fi in group_sorted[1:])  # todos menos el mas nuevo

        if not to_delete:
            messagebox.showinfo("Nada que hacer", "No hay archivos antiguos para eliminar.")
            return

        destino = "la Papelera de reciclaje" if HAS_SEND2TRASH else "de forma PERMANENTE"
        if not messagebox.askyesno(
            "Confirmar eliminacion",
            f"Se eliminaran {len(to_delete)} archivo(s) duplicado(s) (los mas antiguos de cada grupo), "
            f"enviandolos a {destino}.\n\n¿Continuar?",
        ):
            return

        deleted, errors = self._delete_paths(to_delete)
        self._report_deletion(deleted, errors)
        self._start_scan()  # re-escanear para refrescar la lista

    def _delete_selected_exact(self):
        selected = self.exact_tree.selection()
        # Solo permitir borrar items hoja (no grupos), y advertir si eligieron "Conservar"
        paths = []
        warned_keep = False
        for iid in selected:
            values = self.exact_tree.item(iid, "values")
            if not values:
                continue  # es un grupo, no un archivo
            estado, path = values[0], values[1]
            if estado.startswith("Conservar"):
                warned_keep = True
            paths.append(path)

        if not paths:
            messagebox.showinfo("Sin seleccion", "Selecciona uno o mas archivos de la lista para eliminar.")
            return

        if warned_keep:
            if not messagebox.askyesno(
                "Atencion",
                "Estas por eliminar el archivo marcado como 'Conservar (mas nuevo)' en algun grupo. "
                "¿Seguro que queres continuar?",
            ):
                return

        destino = "la Papelera de reciclaje" if HAS_SEND2TRASH else "de forma PERMANENTE"
        if not messagebox.askyesno(
            "Confirmar eliminacion",
            f"Se eliminaran {len(paths)} archivo(s), enviandolos a {destino}.\n\n¿Continuar?",
        ):
            return

        deleted, errors = self._delete_paths(paths)
        self._report_deletion(deleted, errors)
        self._start_scan()

    def _report_deletion(self, deleted, errors):
        msg = f"Se eliminaron {deleted} archivo(s)."
        if errors:
            msg += f"\n\nHubo {len(errors)} error(es):\n" + "\n".join(errors[:10])
        messagebox.showinfo("Resultado", msg)

    # ------------------------- renombrar (mismo nombre, distinto contenido) -------------------------

    def _rename_selected(self):
        selected = self.name_tree.selection()
        if not selected:
            messagebox.showinfo("Sin seleccion", "Selecciona un archivo de la lista para renombrar.")
            return
        iid = selected[0]
        values = self.name_tree.item(iid, "values")
        if not values:
            messagebox.showinfo("Selecciona un archivo", "Elegi un archivo dentro de un grupo, no el grupo en si.")
            return
        old_path = values[0]
        folder, old_name = os.path.split(old_path)
        base, ext = os.path.splitext(old_name)

        new_name = simpledialog.askstring(
            "Renombrar archivo",
            f"Carpeta: {folder}\n\nNuevo nombre para:\n{old_name}",
            initialvalue=f"{base}_v2{ext}",
        )
        if not new_name:
            return
        new_path = os.path.join(folder, new_name)
        if os.path.exists(new_path):
            messagebox.showerror("Error", "Ya existe un archivo con ese nombre en la misma carpeta.")
            return
        try:
            os.rename(old_path, new_path)
        except OSError as e:
            messagebox.showerror("Error al renombrar", str(e))
            return
        messagebox.showinfo("Listo", f"Renombrado a:\n{new_path}")
        self._start_scan()


def main():
    app = DuplicateFinderApp()
    app.mainloop()


if __name__ == "__main__":
    main()
