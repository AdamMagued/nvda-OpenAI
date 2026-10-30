"""Saving, loading, renaming conversations and the dialog's data.json."""

import json
import os
import gui
import wx
import addonHandler
import ui
from logHandler import log
from . import conversations
from .consts import DATA_DIR
from .image_file import AttachmentFile

addonHandler.initTranslation()

DATA_JSON_FP = os.path.join(DATA_DIR, "data.json")


class ConversationPersistenceMixin:
	"""Mixin for ConversationDialog."""

	def _collect_blocks_from_page(self, page):
		blocks = []
		b = page.firstBlock
		while b:
			blocks.append(b)
			b = b.next
		return blocks

	def _model_id_from_block_chain(self, page):
		"""First non-empty model id on the block chain (tail toward head, then forward)."""
		b = page.lastBlock
		while b:
			mid = (getattr(b, "model", None) or "").strip()
			if mid:
				return mid
			b = getattr(b, "previous", None)
		b = page.firstBlock
		while b:
			mid = (getattr(b, "model", None) or "").strip()
			if mid:
				return mid
			b = getattr(b, "next", None)
		return ""

	def _conversation_record_for_page(self, page):
		cid = getattr(page, "_conversationId", None)
		if not cid:
			return None
		try:
			return conversations.load_conversation(cid)
		except Exception:
			log.debug("Ignored error", exc_info=True)
			return None

	def _chrome_hints_for_page(self, page):
		"""Resolve model id and account key for shared chrome: blocks, tab hints, then one disk read."""
		model_id = self._model_id_from_block_chain(page)
		if not model_id:
			model_id = (getattr(page, "conversationModelHint", None) or "").strip()
		account_key = (getattr(page, "conversationAccountKey", None) or "").strip()
		rec = None
		if not model_id or not account_key:
			rec = self._conversation_record_for_page(page)
		if rec:
			if not model_id:
				model_id = (rec.get("model") or "").strip()
			if not account_key:
				account_key = (rec.get("accountKey") or "").strip()
		return model_id, account_key

	def _model_id_for_page_persist(self, page):
		return self._model_id_from_block_chain(page) or (getattr(page, "conversationModelHint", None) or "").strip()

	def _default_title_for_new_conversation(self, blocks, draft_stripped):
		if blocks:
			first = blocks[0]
			prompt = getattr(first, "prompt", "") or ""
			tlist = getattr(first, "audioTranscriptList", None)
			if not prompt and tlist and any(t for t in tlist):
				prompt = "\n".join(t for t in tlist if t).strip()
		else:
			prompt = draft_stripped
		return conversations.get_default_title(prompt)

	def _page_has_restorable_content(self, page, *, blocks=None, draft_prompt=None):
		"""A page should be restored only when it has message history or a non-empty draft."""
		if blocks is None:
			blocks = self._collect_blocks_from_page(page)
		if draft_prompt is None:
			draft_prompt = page.promptTextCtrl.GetValue()
		return bool(blocks) or bool((draft_prompt or "").strip())

	def _storage_kwargs_for_page(self, page, *, autosave_force=False, for_autosave=False):
		"""
		Build arguments for conversations.save_conversation, or None if nothing should be written.
		Persist allows draft-only tabs; periodic autosave skips draft-only unless autosave_force (manual save).
		A tab is persisted only when it has message history or a non-empty draft prompt.
		"""
		if getattr(page, "ephemeral", False):
			return None
		blocks = self._collect_blocks_from_page(page)
		draft_prompt = page.promptTextCtrl.GetValue()
		draft_stripped = draft_prompt.strip()
		if not self._page_has_restorable_content(page, blocks=blocks, draft_prompt=draft_prompt):
			return None
		if not blocks and for_autosave and not autosave_force:
			return None
		if self.conf.get("saveSystem"):
			system = (getattr(page, "conversationSystemText", None) or "").strip()
		else:
			system = ""
		model_id = self._model_id_for_page_persist(page)
		account_key = (getattr(page, "conversationAccountKey", None) or "").strip()
		ui_state = getattr(page, "conversationUiState", None)
		if not isinstance(ui_state, dict):
			ui_state = {}
		name = None
		if not page._conversationId:
			name = self._default_title_for_new_conversation(blocks, draft_stripped)
		return {
			"blocks": blocks,
			"system": system,
			"model": model_id,
			"name": name,
			"conv_id": page._conversationId,
			"draftPrompt": draft_prompt,
			"draftPathList": page.filesList,
			"draftAudioPathList": page.audioPathList,
			"account_key": account_key,
			"ui_state": ui_state,
			"usage_ledger": list(getattr(page, "usageLedger", None) or []),
			"detached_branch": getattr(page, "detachedBranch", None),
			"prompt_cache_key": getattr(page, "_promptCacheKey", None) or "",
		}

	def _saveConversationFromKw(self, kw):
		return conversations.save_conversation(
			kw["blocks"],
			system=kw["system"],
			model=kw["model"],
			name=kw["name"],
			conv_id=kw["conv_id"],
			draftPrompt=kw["draftPrompt"],
			draftPathList=kw["draftPathList"],
			draftAudioPathList=kw["draftAudioPathList"],
			account_key=kw["account_key"],
			ui_state=kw["ui_state"],
			usage_ledger=kw.get("usage_ledger"),
			detached_branch=kw.get("detached_branch"),
			prompt_cache_key=kw.get("prompt_cache_key") or "",
		)

	def loadData(self):
		if not os.path.exists(DATA_JSON_FP):
			return {}
		try:
			with open(DATA_JSON_FP, "r", encoding="utf-8") as f:
				data = json.load(f)
			return data if isinstance(data, dict) else {}
		except Exception as err:
			log.error(f"loadData: {err}", exc_info=True)
			return {}

	def saveData(self, force=False):
		if not isinstance(self.data, dict):
			return
		if not force and self.data == self._orig_data:
			return
		tmp_path = DATA_JSON_FP + ".tmp"
		try:
			with open(tmp_path, "w", encoding="utf-8") as f:
				json.dump(self.data, f, indent=2, ensure_ascii=False)
			os.replace(tmp_path, DATA_JSON_FP)
		except Exception as err:
			log.error(f"saveData: {err}", exc_info=True)

	def _loadConversation(self, data, *, focus_message_history=False):
		"""Load conversation data (blocks, system) from saved conversation."""
		blocks = data.get("blocks", [])
		if not isinstance(blocks, list):
			blocks = []
		# Translators: Fallback title for loaded conversations missing a name.
		conv_name = data.get("name", _("Untitled conversation"))
		idx_tab = self.notebook.GetSelection()
		if idx_tab >= 0:
			self.notebook.SetPageText(idx_tab, conv_name)
		active_pg = self.get_active_page()
		active_pg.conversationModelHint = (data.get("model") or "").strip()
		active_pg.conversationAccountKey = (data.get("accountKey") or "").strip()
		raw_ui = data.get("uiState")
		active_pg.conversationUiState = raw_ui if isinstance(raw_ui, dict) else {}
		active_pg.ephemeral = False
		active_pg.conversationSystemText = data.get("system", "") if isinstance(data.get("system"), str) else ""
		from .usage_ledger import deserialize_ledger, migrate_ledger_from_blocks

		raw_ledger = data.get("usageLedger")
		if isinstance(raw_ledger, list) and raw_ledger:
			active_pg.usageLedger = deserialize_ledger(raw_ledger)
		else:
			active_pg.usageLedger = migrate_ledger_from_blocks(blocks)
		raw_detached = data.get("detachedBranch")
		active_pg.detachedBranch = raw_detached if isinstance(raw_detached, dict) else None
		self._clearMessagesSegments()
		self.firstBlock = None
		self.lastBlock = None
		system = data.get("system", "")
		if system and self.conf["saveSystem"]:
			self.systemTextCtrl.ChangeValue(system)
			self._lastSystem = system
		draft_prompt = data.get("draftPrompt", "")
		if isinstance(draft_prompt, str) and draft_prompt:
			self.promptTextCtrl.SetValue(draft_prompt)
			self.promptTextCtrl.SetInsertionPointEnd()
		draft_path_list = data.get("draftPathList", [])
		self.filesList = []
		if isinstance(draft_path_list, list):
			for item in draft_path_list:
				if isinstance(item, dict):
					path = item.get("path", "")
					name = item.get("name", "")
					if path:
						try:
							att = AttachmentFile(path, name=name or None)
							conversations._apply_file_ids(att, item)
							self.filesList.append(att)
						except Exception as err:
							log.warning(f"load draft image skipped {path}: {err}")
				elif isinstance(item, str) and item:
					try:
						self.filesList.append(AttachmentFile(item))
					except Exception as err:
						log.warning(f"load draft image skipped {item}: {err}")
		draft_audio_list = data.get("draftAudioPathList", [])
		self.audioPathList = []
		if isinstance(draft_audio_list, list):
			for item in draft_audio_list:
				if isinstance(item, str) and item:
					self.audioPathList.append(item)
		self.updateFilesList(focusPrompt=False)
		self.updateAudioList(focusPrompt=False)
		conv_id = data.get("id")
		if conv_id:
			self._conversationId = conv_id
		cache_key = data.get("promptCacheKey") or conv_id
		if isinstance(cache_key, str) and cache_key.strip():
			active_pg._promptCacheKey = cache_key.strip()
		else:
			active_pg._promptCacheKey = None
		if not blocks:
			self._syncWindowTitleFromActiveTab()
			if focus_message_history:
				self._scheduleFocusMessageHistory()
			return
		prev = None
		for b in blocks:
			b.lastLen = len(b.responseText or "")
			b.lastReasoningLen = len(b.reasoningText or "")
			b.displayHeader = False
			if prev is not None:
				prev.next = b
				b.previous = prev
			else:
				self.firstBlock = b
			prev = b
		self.lastBlock = prev
		if active_pg.detachedBranch:
			from .detached_branch import clear_detached_branch, find_block_by_uid

			anchor = find_block_by_uid(self.firstBlock, active_pg.detachedBranch.get("anchorBlockId"))
			if anchor is None:
				clear_detached_branch(active_pg)
		for b in blocks:
			self._appendBlockToMessages(b)
		if self.firstBlock and self.firstBlock.segmentPrompt is not None:
			self.messagesTextCtrl.SetInsertionPoint(self.firstBlock.segmentPrompt.start)
		self._syncWindowTitleFromActiveTab()
		if focus_message_history:
			self._scheduleFocusMessageHistory()
		if self.get_active_page() is active_pg:
			self._syncEphemeralChromeFromPage(active_pg)

	def _getBlocksForSave(self):
		"""Collect all blocks from firstBlock to lastBlock for saving."""
		return self._collect_blocks_from_page(self.get_active_page())

	def _autoSaveConversation(self, force=False):
		"""Save conversation state. Auto-save obeys setting unless forced (manual save)."""
		pg = self.get_active_page()
		if getattr(pg, "ephemeral", False):
			return False
		if not force and not self.conf.get("autoSaveConversation", True):
			return False
		self._captureConversationChromeToPage(pg)
		kw = self._storage_kwargs_for_page(pg, for_autosave=True, autosave_force=force)
		if kw is None:
			return False
		try:
			conv_id = self._saveConversationFromKw(kw)
			self._conversationId = conv_id
			new_title = kw.get("name")
			if new_title:
				idx = self.notebook.GetSelection()
				if idx >= 0:
					self.notebook.SetPageText(idx, new_title)
				self._syncWindowTitleFromActiveTab()
			return True
		except Exception as err:
			log.error(f"auto-save conversation: {err}", exc_info=True)
			return False

	def _saveConversation(self, evt=None):
		"""Save current conversation to storage (manual, from menu)."""
		if self._is_active_tab_ephemeral():
			return
		blocks = self._getBlocksForSave()
		draft_prompt = self.promptTextCtrl.GetValue().strip()
		if not blocks and not draft_prompt:
			# Translators: Message when manual save is requested for an empty conversation.
			ui.message(_("No messages or prompt to save."))
			return
		if self._autoSaveConversation(force=True):
			# Translators: Confirmation message after successful manual save.
			ui.message(_("Conversation saved."))
		else:
			gui.messageBox(
				# Translators: Error text when manual conversation save fails.
				_("Unable to save conversation. Check the NVDA log for details."),
				# Translators: Title for manual save error dialog.
				_("Save conversation"),
				wx.OK | wx.ICON_ERROR
			)

	def _renameConversation(self, evt=None):
		"""Rename current conversation."""
		if getattr(self.get_active_page(), "ephemeral", False):
			# Translators: Message when rename is blocked because ephemeral mode is on.
			ui.message(_("Ephemeral conversations cannot be renamed. Uncheck Ephemeral to save first."))
			return
		if not self._conversationId:
			# Translators: Message shown when trying to rename before first save.
			ui.message(_("Save the conversation first before renaming."))
			return
		entry = next((e for e in conversations.list_conversations() if e.get("id") == self._conversationId), None)
		# Translators: Default name suggested in rename dialog for untitled conversations.
		current_name = entry.get("name", _("Untitled conversation")) if entry else ""
		dlg = wx.TextEntryDialog(
			self,
			# Translators: Prompt text in rename conversation dialog.
			_("Enter new name for this conversation:"),
			# Translators: Title of rename conversation dialog.
			_("Rename conversation"),
			value=current_name
		)
		if dlg.ShowModal() != wx.ID_OK:
			dlg.Destroy()
			return
		new_name = dlg.GetValue().strip()
		dlg.Destroy()
		if not new_name:
			return
		if conversations.rename_conversation(self._conversationId, new_name):
			idx = self.notebook.GetSelection()
			if idx >= 0:
				self.notebook.SetPageText(idx, new_name)
			self._syncWindowTitleFromActiveTab()

	def _onConversationList(self, evt=None):
		"""Open the conversation history dialog."""
		from .conversations_manager_dialog import show_conversations_manager
		if self._plugin:
			wx.CallAfter(show_conversations_manager, self._plugin)

	def _newConversation(self, evt=None):
		"""Open a new conversation tab."""
		self._addConversationTab()
		wx.CallAfter(self.promptTextCtrl.SetFocus)
		# Translators: Spoken status message after creating a new conversation tab.
		ui.message(_("New conversation"))
