import cv2
import numpy as np
import time
import os
import json
import pyautogui  # pip install pyautogui
import requests
import threading
import tkinter as tk
from io import StringIO
from openai import OpenAI

# python-hslog 库用于解析炉石日志
from hslog import LogParser
from hslog.export import EntityTreeExporter, FriendlyPlayerExporter
from hearthstone.enums import GameTag, Zone, CardType, Mulligan, Step, BlockType, CardClass
from hearthstone import cardxml
from hearthstone.deckstrings import parse_deckstring


class GameStateTracker:
    """使用 python-hslog 库解析炉石日志并追踪游戏状态"""
    
    def __init__(self):
        print("[*] 正在加载炉石卡牌数据库 (zhCN)...")
        try:
            self.card_db, _ = cardxml.load(locale="zhCN")
            print(f"[*] 卡牌数据库加载完成: {len(self.card_db)} 张卡牌")
            # 建立 DBID 到卡牌对象的映射
            self.dbid_map = {c.dbf_id: c for c in self.card_db.values()}
        except Exception as e:
            print(f"[!] 加载卡牌数据库失败: {e}")
            self.card_db = {}
            
        self.reset()

# ... (omitted methods) ...

    def get_hero_state(self, player_id):
        """获取指定玩家的英雄状态 (包含中文描述和职业)"""
        if not self.game:
            return {"health": 30, "armor": 0, "atk": 0, "name": "Unknown", "class": "UNKNOWN"}
            
        player = self.game.players[player_id - 1]
        hero = None
        for e in player.entities:
            if e.tags.get(GameTag.ZONE) == Zone.PLAY and e.tags.get(GameTag.CARDTYPE) == CardType.HERO:
                hero = e
                break
                
        if not hero:
            return {"health": 0, "armor": 0, "atk": 0, "name": "Unknown", "class": "UNKNOWN"}
            
        tags = hero.tags
        card_info = self.get_card_data(hero.card_id)
        
        # 获取职业
        class_enum = tags.get(GameTag.CLASS, 0)
        try:
            class_name = CardClass(class_enum).name
        except:
            class_name = "UNKNOWN"
        
        return {
            "name": card_info["name"],
            "text": card_info["text"],
            "health": tags.get(GameTag.HEALTH, 30) - tags.get(GameTag.DAMAGE, 0),
            "armor": tags.get(GameTag.ARMOR, 0),
            "atk": tags.get(GameTag.ATK, 0),
            "class": class_name
        }

# ... (omitted methods) ...

    def decide_action(self, state):
        """请求 OpenAI 返回 JSON 格式的操作指令"""
        if not state:
            return None

        if state.get("game_over"):
            return None

        self.log("[*] AI 正在思考中...")

        # 针对 Mulligan 阶段的特殊 Prompt
        if state.get("game_phase") == "MULLIGAN":
            prompt = f"""
            你是一个炉石传说AI助手。现在是起手调度阶段 (Mulligan)。
            
            你的手牌: {state.get("hand_cards", [])}
            你的职业: {state.get("my_hero", {}).get("class", "UNKNOWN")}
            对手职业: {state.get("enemy_hero", {}).get("class", "UNKNOWN")}
            
            请决定替换掉哪些牌。
            策略：
            1. 寻找低费随从（1-3费）以确保前期节奏。
            2. 根据对手职业考虑保留针对牌（例如对抗快攻保留解场，对抗慢速保留过牌/OTK组件）。
            
            请输出 JSON 响应，格式如下：
            {{
                "thought": "简短的一句话思考过程",
                "actions": [
                    {{ "type": "MULLIGAN_REPLACE", "hand_index": 0, "desc": "替换第1张" }},
                    {{ "type": "MULLIGAN_CONFIRM", "desc": "确认" }}
                ]
            }}
            注意：如果不替换任何牌，直接输出 MULLIGAN_CONFIRM。
            只输出 JSON。
            """
        else:
            # 常规回合 Prompt - 增强版智能
            prompt = f"""
            Role: You are a professional Hearthstone player aiming for the highest win rate.
            
            Matchup Info:
            - My Class: {state.get("my_hero", {}).get("class", "UNKNOWN")}
            - Opponent Class: {state.get("enemy_hero", {}).get("class", "UNKNOWN")}
            
            Current Game State (JSON):
            {json.dumps(state, ensure_ascii=False)}
            
            Your Strategic Priorities (in order):
            1. **HANDLE CHOICES**: If "choices" list is present and not empty, you MUST return a "CHOOSE" action immediately.
            2. **CHECK LETHAL**: Calculate damage carefully. If LETHAL is possible, ignore trading and go FACE.
            3. **MATCHUP ANALYSIS**: 
               - Consider the opponent's class capabilities (e.g., Mage has Flamestrike at 7 mana, Priest has removal/healing).
               - Anticipate their likely next turn. Don't overextend into AOE if unnecessary.
            4. **SURVIVAL**: If Health is low, prioritize Taunt, Healing, or Clearing dangerous minions.
            5. **VALUE TRADING**: Make favorable trades. 
            6. **TEMPO**: Spend Mana efficiently to develop the board.
            
            Rules:
            - Minions with "exhausted": true cannot attack.
            - You cannot target "immune" or "stealth" characters.
            
            Output Format:
            Provide a strictly valid JSON response.
            IMPORTANT: Your 'thought' content MUST be in CHINESE and include strategic reasoning about the matchup.
            {{
                "thought": "Deep strategic thinking in Chinese. Should mention opponent class and key considerations. E.g., '对手是法师，可能游荡恶鬼，我需要...'",
                "actions": [
                    {{ "type": "CHOOSE", "index": 0, "desc": "Reason" }},
                    {{ "type": "PLAY_MINION", "hand_index": 0, "desc": "Desc" }},
                    {{ "type": "ATTACK", "attacker_index": 0, "target_type": "minion", "target_index": 0, "desc": "Trade" }},
                    {{ "type": "END_TURN", "desc": "End" }}
                ]
            }}
            """
        
        # ... (rest of the method unchanged)
        
    def reset(self):
        """重置游戏状态"""
        self.parser = LogParser()
        self.game = None
        self.friendly_player_id = None
        self.current_choices = []
        self._log_buffer = ""  # 累积日志缓冲区
        self._revealed_cards = {} # 记忆已揭示的实体 ID -> CardID
        self.initial_deck = []    # 初始套牌列表 (开局即确定)
        self.deck_code_list = []  # 从 Deck Code 解析出的完整列表 (带描述)

    def apply_deck_code(self, deck_code: str):
        """解析套牌代码并预填充初始套牌信息"""
        if not deck_code:
            return
        try:
            # 兼容带有 Sideboard (如 E.T.C./奇利亚斯) 的新版 deckstrings
            # parse_deckstring 根据版本可能返回 (cards, heroes, format) 或包含 sideboards 等
            result = parse_deckstring(deck_code)
            cards = result[0]
            
            full_list = []
            for dbid, count in cards:
                card_obj = self.dbid_map.get(dbid)
                if card_obj:
                    # 使用 get_card_data 的格式
                    card_info = self.get_card_data(card_obj.id)
                    item = f"{card_info['name']}: {card_info['text']}"
                    for _ in range(count):
                        full_list.append(item)
            
            self.deck_code_list = sorted(full_list)
            # 如果目前没有 initial_deck，先用这个占位
            if not self.initial_deck:
                self.initial_deck = self.deck_code_list
            print(f"[*] 套牌代码解析成功: 共 {len(full_list)} 张卡牌")
        except Exception as e:
            print(f"[!] 解析套牌代码失败: {e}")
        
    def process_log_chunk(self, content: str):
        """解析日志块，更新实体状态
        
        hslog 设计用于解析完整日志文件，因此我们采用累积缓冲区策略：
        将新内容追加到缓冲区，每次重新解析整个缓冲区。
        """
        if not content:
            return
            
        # 累积日志内容
        self._log_buffer += content
        
        try:
            # 使用 hslog 解析器读取累积的日志
            self.parser = LogParser()  # 每次创建新解析器
            self.parser.read(StringIO(self._log_buffer))
            
            # 如果有游戏，导出当前状态（总是导出最后一个游戏）
            if self.parser.games:
                packet_tree = self.parser.games[-1]
                exporter_result = packet_tree.export()
                # export() 返回 EntityTreeExporter，真正的 Game 对象在 .game 属性中
                self.game = exporter_result.game if hasattr(exporter_result, 'game') else exporter_result
                
                # 检测友方玩家（每个新游戏都需要重新检测）
                exporter = FriendlyPlayerExporter(packet_tree)
                result = exporter.export()
                if result:
                    self.friendly_player_id = result
                
                # 提取当前选择项（发现/抉择）
                self._extract_choices(packet_tree)

                # [NEW] 遍历所有包，更新已揭示的实体信息
                self._update_revealed_cache(packet_tree)
                
                # [NEW] 如果初始套牌还没记录，记录一下
                if not self.initial_deck:
                    # 开局时获取带有完整描述的列表
                    current_deck = self.get_my_deck(include_details=True)
                    if len(current_deck) > 20: 
                        self.initial_deck = current_deck
                        
        except Exception as e:
            print(f"[!] hslog 解析出错: {e}")
    
    def _extract_choices(self, packet_tree):
        """从 PacketTree 中提取当前的选择项"""
        self.current_choices = []
        
        # 遍历最后的几个 packet 寻找 Choices
        if not packet_tree.packets:
            return
            
        # 寻找最后一个 Choices 包
        last_choice_packet = None
        has_response = False
        
        # 追踪回合
        current_turn = 0
        choice_turn = -1
        
        def find_last_choice(packets, depth=0):
            nonlocal last_choice_packet, has_response, current_turn, choice_turn
            if depth > 50: return # 增加深度限制
            
            for packet in packets:
                # 检查包类型 (兼容 hslog 类名)
                pkt_type = type(packet).__name__
                
                # 检测回合切换 (TagChange: Entity=1 (Game), Tag=TURN)
                if pkt_type == 'TagChange':
                    if packet.tag == GameTag.TURN and packet.entity == 1:
                        current_turn = packet.value
                
                if pkt_type == 'Choices':
                    last_choice_packet = packet
                    choice_turn = current_turn # 记录产生 Choice 时的回合
                    has_response = False # 新的 Choice 出现，重置响应状态
                
                elif pkt_type == 'SendChoices':
                    # 如果出现了 SendChoices，说明之前的 Choice 已经被处理
                    has_response = True
                
                # 递归 (注意：从 Block 中递归可能会错过外层的 Turn 变更，但通常 Turn 变更是顶层事件)
                # 即使错过，我们主要关心 Choice 和随后发生的事件。
                if hasattr(packet, 'packets'):
                    find_last_choice(packet.packets, depth + 1)
                    
        try:
            find_last_choice(packet_tree.packets)
            
            # 判定条件：
            # 1. 找到了 Choice
            # 2. 没有被响应 (SendChoices)
            # 3. Choice 发生在当前回合 (防止读取到历史 Choice)
            if last_choice_packet and not has_response and choice_turn == current_turn:
                # 提取选项
                source = getattr(last_choice_packet, 'source', None) # 谁的选择
                choices = getattr(last_choice_packet, 'choices', [])
                
                # 只有当这确实是我们的选择时 (source == friendly_player_id)
                # 但 Log 中 source 往往是 EntityID。需校验。
                # 简单起见，只要有未响应的 Choice，我们就认为是我们的 (通常 Log 只记录可见的 choice)
                
                if choices:
                    print(f"[*] 检测到未处理的 Choices (Turn {choice_turn}): {choices}")
                    for choice_id in choices:
                        entity = self._get_entity_by_id(choice_id)
                        if entity:
                            card_id = self._get_entity_name(entity)
                            card_name = "Unknown"
                            if card_id:
                                data = self.get_card_data(card_id)
                                card_name = data.get("name", card_id)
                            
                            self.current_choices.append({
                                "id": choice_id,
                                "card_id": card_id,
                                "name": card_name
                            })
        except Exception as e:
            print(f"[!] 提取 Choices 出错: {e}")

    def _update_revealed_cache(self, packet_tree):
        """解析所有 Packet，记录已知的实体卡片 ID"""
        def visit_packets(packets, depth=0):
            if depth > 50: return
            for packet in packets:
                # 记录 FullEntity 和 ShowEntity 中的 CardID
                pkt_type = type(packet).__name__
                if pkt_type in ['FullEntity', 'ShowEntity']:
                    if hasattr(packet, 'card_id') and packet.card_id:
                        self._revealed_cards[packet.entity] = packet.card_id
                
                if hasattr(packet, 'packets'):
                    visit_packets(packet.packets, depth + 1)
        
        visit_packets(packet_tree.packets)
    
    def _get_entity_by_id(self, entity_id):
        """根据 ID 获取实体"""
        if not self.game:
            return None
        return self.game.find_entity_by_id(entity_id)
    
    def _get_entity_name(self, entity):
        """获取实体名称 (CardID)"""
        if hasattr(entity, 'card_id') and entity.card_id:
            return entity.card_id
        return None
    
    def _get_entity_zone(self, entity):
        """获取实体所在区域"""
        if not entity:
            return None
        return entity.tags.get(GameTag.ZONE)
    
    def _get_entity_controller(self, entity):
        """获取实体控制者"""
        if not entity:
            return None
        return entity.tags.get(GameTag.CONTROLLER)
    
    def _is_minion(self, entity):
        """检查实体是否为随从"""
        if not entity:
            return False
        card_type = entity.tags.get(GameTag.CARDTYPE)
        return card_type == CardType.MINION
    
    def _is_hero(self, entity):
        """检查实体是否为英雄"""
        if not entity:
            return False
        card_type = entity.tags.get(GameTag.CARDTYPE)
        return card_type == CardType.HERO
    
    def _simplify(self, entity):
        """将实体转换为简化的字典格式"""
        tags = entity.tags
        health = tags.get(GameTag.HEALTH, 0)
        damage = tags.get(GameTag.DAMAGE, 0)
        
        return {
            "name": self._get_entity_name(entity) or "Unknown",
            "atk": tags.get(GameTag.ATK, 0),
            "health": health - damage,
            "cost": tags.get(GameTag.COST, 0),
            "divine_shield": tags.get(GameTag.DIVINE_SHIELD, 0) == 1,
            "taunt": tags.get(GameTag.TAUNT, 0) == 1,
            "exhausted": tags.get(GameTag.EXHAUSTED, 0) == 1
        }
    
    def get_card_data(self, card_id):
        """获取卡牌详细信息 (中文)"""
        if not card_id:
            return {"name": "Unknown", "text": ""}
        
        card = self.card_db.get(card_id)
        if card:
            # 清理描述文本中的 HTML 标签 (如 <b>, <i>)
            description = card.description or ""
            description = description.replace("<b>", "").replace("</b>", "")
            description = description.replace("<i>", "").replace("</i>", "")
            description = description.replace("$", "") # 移除变量占位符
            description = description.replace("[x]", "")
            description = description.replace("\\n", "")
            return {
                "name": card.name,
                "text": description,
                "cost": card.cost,
                "type": card.type
            }
        return {"name": card_id, "text": ""}

    def get_choices(self):
        """获取当前的选择项"""
        return self.current_choices
    
    def get_my_hand(self):
        """获取己方手牌 (包含中文描述)"""
        if not self.game or not self.friendly_player_id:
            return []
            
        player = self.game.players[self.friendly_player_id - 1]
        hand_zone = [e for e in player.entities if e.tags.get(GameTag.ZONE) == Zone.HAND]
        
        # 排序：按场上位置 (ZONE_POSITION)
        hand_zone.sort(key=lambda x: x.tags.get(GameTag.ZONE_POSITION, 0))
        
        cards = []
        for e in hand_zone:
            card_id = e.card_id
            card_info = self.get_card_data(card_id)
            
            cards.append({
                "id": card_id,
                "name": card_info["name"], # 中文名
                "text": card_info["text"], # 描述
                "atk": e.tags.get(GameTag.ATK, 0),
                "health": e.tags.get(GameTag.HEALTH, 0) - e.tags.get(GameTag.DAMAGE, 0),
                "cost": e.tags.get(GameTag.COST, 0),
                "divine_shield": e.tags.get(GameTag.DIVINE_SHIELD, 0) == 1,
                "taunt": e.tags.get(GameTag.TAUNT, 0) == 1,
                "exhausted": e.tags.get(GameTag.EXHAUSTED, 0) == 1
            })
        return cards
    
    def get_my_board(self):
        """获取己方场面 (包含中文描述)"""
        if not self.game or not self.friendly_player_id:
            return []
            
        player = self.game.players[self.friendly_player_id - 1]
        board_zone = [e for e in player.entities if e.tags.get(GameTag.ZONE) == Zone.PLAY and e.tags.get(GameTag.CARDTYPE) == CardType.MINION]
        board_zone.sort(key=lambda x: x.tags.get(GameTag.ZONE_POSITION, 0))
        
        minions = []
        for e in board_zone:
            card_info = self.get_card_data(e.card_id)
            minions.append({
                "name": card_info["name"],
                "text": card_info["text"],
                "atk": e.tags.get(GameTag.ATK, 0),
                "health": e.tags.get(GameTag.HEALTH, 0) - e.tags.get(GameTag.DAMAGE, 0),
                "divine_shield": e.tags.get(GameTag.DIVINE_SHIELD, 0) == 1,
                "taunt": e.tags.get(GameTag.TAUNT, 0) == 1,
                "can_attack": e.tags.get(GameTag.EXHAUSTED, 0) == 0 and e.tags.get(GameTag.FROZEN, 0) == 0
            })
        return minions
    
    def get_opp_board(self):
        """获取对方场面 (包含中文描述)"""
        if not self.game or not self.friendly_player_id:
            return []
            
        opp_id = 3 - self.friendly_player_id
        player = self.game.players[opp_id - 1]
        board_zone = [e for e in player.entities if e.tags.get(GameTag.ZONE) == Zone.PLAY and e.tags.get(GameTag.CARDTYPE) == CardType.MINION]
        board_zone.sort(key=lambda x: x.tags.get(GameTag.ZONE_POSITION, 0))
        
        minions = []
        for e in board_zone:
            card_info = self.get_card_data(e.card_id)
            minions.append({
                "name": card_info["name"],
                "text": card_info["text"],
                "atk": e.tags.get(GameTag.ATK, 0),
                "health": e.tags.get(GameTag.HEALTH, 0) - e.tags.get(GameTag.DAMAGE, 0),
                "divine_shield": e.tags.get(GameTag.DIVINE_SHIELD, 0) == 1,
                "taunt": e.tags.get(GameTag.TAUNT, 0) == 1
            })
        return minions
    
    def get_hero_state(self, player_id):
        """获取指定玩家的英雄状态 (包含中文描述)"""
        if not self.game:
            return {"health": 30, "armor": 0, "atk": 0, "name": "Unknown"}
            
        player = self.game.players[player_id - 1]
        hero = None
        for e in player.entities:
            if e.tags.get(GameTag.ZONE) == Zone.PLAY and e.tags.get(GameTag.CARDTYPE) == CardType.HERO:
                hero = e
                break
                
        if not hero:
            return {"health": 0, "armor": 0, "atk": 0, "name": "Unknown"}
            
        tags = hero.tags
        card_info = self.get_card_data(hero.card_id)
        
        return {
            "name": card_info["name"],
            "text": card_info["text"],
            "health": tags.get(GameTag.HEALTH, 30) - tags.get(GameTag.DAMAGE, 0),
            "armor": tags.get(GameTag.ARMOR, 0),
            "atk": tags.get(GameTag.ATK, 0)
        }

    def get_my_deck(self, include_details=False):
        """获取己方牌库剩余卡牌"""
        if not self.game or not self.friendly_player_id:
            return []
            
        player = self.game.players[self.friendly_player_id - 1]
        
        # 基础逻辑：统计当前 ZONE 为 DECK 的实体
        deck_entities = [e for e in player.entities if e.tags.get(GameTag.ZONE) == Zone.DECK]
        
        # 如果有套牌代码提供的完整列表，尝试进行“排除法”推理
        if self.deck_code_list:
            full_list_names = []
            for item in self.deck_code_list:
                name = item.split(":")[0] if ":" in item else item
                full_list_names.append(name)
            
            # 统计所有【已揭示】且【不在牌库】的卡牌名称
            drawn_cards = []
            for e in player.entities:
                if e.tags.get(GameTag.ZONE) != Zone.DECK:
                    card_id = e.card_id or self._revealed_cards.get(e.id)
                    if card_id:
                        card_info = self.get_card_data(card_id)
                        drawn_cards.append(card_info["name"])
            
            # 从完整名单中移除这些已抽到的牌
            from copy import deepcopy
            remaining_names = deepcopy(full_list_names)
            for card in drawn_cards:
                if card in remaining_names:
                    remaining_names.remove(card)
            
            if include_details:
                # 从 deck_code_list 中捞出匹配项
                final_deck = []
                temp_remaining = deepcopy(remaining_names)
                for item in self.deck_code_list:
                    name = item.split(":")[0] if ":" in item else item
                    if name in temp_remaining:
                        final_deck.append(item)
                        temp_remaining.remove(name)
                return final_deck
            else:
                from collections import Counter
                counts = Counter(remaining_names)
                return [f"{name} x{count}" for name, count in sorted(counts.items())]

        # --- 原始解析逻辑 (无套牌代码时的保底) ---
        cards = []
        unknown_count = 0
        for e in deck_entities:
            card_id = e.card_id or self._revealed_cards.get(e.id)
            if card_id:
                card_info = self.get_card_data(card_id)
                if include_details:
                    cards.append(f"{card_info['name']}: {card_info['text']}")
                else:
                    cards.append(card_info["name"])
            else:
                unknown_count += 1
                
        from collections import Counter
        counts = Counter(cards)
        
        if include_details:
            deck_list = sorted(list(set(cards))) 
        else:
            deck_list = [f"{name} x{count}" for name, count in sorted(counts.items())]
        
        if unknown_count > 0:
            deck_list.append(f"未知卡牌 x{unknown_count}")
            
        return deck_list

    
    def get_game_entity(self):
        """获取游戏实体（包含回合、法力等全局信息）"""
        if not self.game:
            return None
        # 游戏实体通常是 ID 为 1 的实体
        return self.game.find_entity_by_id(1)
    
    def get_player_entity(self, player_id):
        """获取玩家实体"""
        if not self.game:
            return None
        # 玩家实体 ID 通常是 2 和 3
        return self.game.find_entity_by_id(player_id + 1)
    
    def get_mana_info(self):
        """获取法力值信息"""
        if not self.game:
            return 0, 0
            
        pid = self.friendly_player_id or 1
        player_entity = self.get_player_entity(pid)
        
        if player_entity:
            tags = player_entity.tags
            resources = tags.get(GameTag.RESOURCES, 0)
            resources_used = tags.get(GameTag.RESOURCES_USED, 0)
            return resources - resources_used, resources
        
        return 0, 0
    
    def get_game_phase(self):
        """获取游戏阶段"""
        if not self.game:
            return "UNKNOWN"
            
        pid = self.friendly_player_id or 1
        player_entity = self.get_player_entity(pid)
        
        if player_entity:
            mulligan_state = player_entity.tags.get(GameTag.MULLIGAN_STATE)
            if mulligan_state in (Mulligan.INPUT, Mulligan.DEALING):
                return "MULLIGAN"
        
        return "PLAYING"
    
    def get_turn(self):
        """获取当前回合数"""
        game_entity = self.get_game_entity()
        if game_entity:
            return game_entity.tags.get(GameTag.TURN, 1)
        return 1



    def is_my_turn(self):
        """判断当前是否为我的回合"""
        if not self.game or not self.friendly_player_id:
            return False
        
        # 检查当前游戏阶段
        # MULLIGAN 阶段特殊处理：始终视为有效，因为需要换牌
        game_entity = self.game
        if game_entity.tags.get(GameTag.STEP) == Step.BEGIN_MULLIGAN:
            return True
            
        # 常规回合检查：当前回合的 CURRENT_PLAYER 是否为我
        try:
            for player in self.game.players:
                if player.player_id == self.friendly_player_id:
                    return player.tags.get(GameTag.CURRENT_PLAYER, 0) == 1
        except Exception as e:
            print(f"[!] Warning checking turn: {e}")
        return False

class LogOverlay:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Hearthstone Copilot Overlay")

        # 窗口属性：置顶、无边框、透明背景
        self.root.attributes("-topmost", True)
        self.root.overrideredirect(True)
        self.root.attributes("-alpha", 0.85)  # 稍微不那么透明，提高可读性
        self.root.configure(bg='black')

        # 初始位置设置为左上角，但不指定大小，让其自动适应
        self.root.geometry("+10+10")

        # 1. 状态标签 (顶部，常驻)
        self.status_label = tk.Label(
            self.root,
            text="等待游戏状态...",
            fg="#00FFFF",  # 青色 Cyan
            bg="black",
            font=("Consolas", 10, "bold"),
            justify="left",
            wraplength=400  # 固定最大宽度
        )
        self.status_label.pack(side="top", fill='x', padx=5, pady=5)

        # 分割线
        self.separator = tk.Frame(self.root, height=1, bd=1,
                             relief="sunken", bg="gray")
        self.separator.pack(fill="x", padx=5, pady=2)

        # 2. 信息标签 (底部，滚动/变动)
        self.info_label = tk.Label(
            self.root,
            text="就绪。",
            fg="white",
            bg="black",
            font=("Consolas", 10),
            justify="left",
            wraplength=400,
            anchor="nw"  # 左上对齐
        )
        self.info_label.pack(side="top", fill='both', padx=5, pady=5)

        # 绑定退出 (双击退出)
        self.root.bind("<Double-Button-1>", lambda e: self.root.quit())
        
        # 强制更新一次布局
        self.root.update_idletasks()

    def update_status(self, text):
        self.status_label.config(text=text)
        self.root.update_idletasks() # 刷新布局

    def update_info(self, text):
        self.info_label.config(text=text)
        self.root.update_idletasks() # 刷新布局以适应新高度

    def update_text(self, text):
        # 兼容旧代码，默认更新信息区
        self.update_info(text)

    def mainloop(self):
        self.root.mainloop()





class HearthstoneAutoPilot:
    def __init__(self, overlay=None, config_path="config.json"):
        self.overlay = overlay
        self.log_overlay = overlay # Alias for consistency
        self.tracker = GameStateTracker()
        self.load_config(config_path)

        self.last_tell = 0
        self.client = OpenAI(
            api_key=self.config["API_KEY"], base_url=self.config["BASE_URL"])
        
        # 应用套牌代码
        if "DECK_CODE" in self.config:
            self.tracker.apply_deck_code(self.config["DECK_CODE"])

        # 安全设置：鼠标移动到屏幕左上角(0,0)可强制终止程序
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 1.0  # 每次点击后暂停1秒，模拟人类迟钝

        self.log(f"[*] ai-hearthstone 已启动。移至屏幕左上角可强制停止。")
        self.last_is_my_turn = False

    def log(self, text):
        print(text)
        if self.overlay:
            # 这是一个简单的线程安全调用方式 (Tkinter 不是完全线程安全的，但 text update 通常 ok，或者用 after)
            # 为了更稳健，我们使用 root.after
            self.overlay.root.after(0, self.overlay.update_text, text)

    def load_config(self, config_path):
        """加载配置文件"""
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                self.config = json.load(f)
            print(f"[*] 配置已加载: {config_path}")
        except Exception as e:
            print(f"[!] 无法加载配置: {e}")
            self.config = {}

    def get_game_state(self):
        """读取日志获取真实状态（使用 hslog 库）"""
        log_path = self.config.get("LOG_PATH", "Power.log")
        if not os.path.exists(log_path):
            return None

        content = ""
        try:
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                f.seek(self.last_tell)
                new_lines = f.readlines()
                if new_lines:
                    self.last_tell = f.tell()
                    content = "".join(new_lines)
        except Exception as e:
            print(f"[!] 读取日志出错: {e}")
            return None

        if not content:
            return None

        # 使用 hslog 解析器更新游戏状态
        self.tracker.process_log_chunk(content)
        
        # 如果没有游戏数据，返回 None
        if not self.tracker.game:
            return None

        # 从 GameStateTracker 获取所有状态信息
        game_phase = self.tracker.get_game_phase()
        turn = self.tracker.get_turn()
        current_mana, resources_max = self.tracker.get_mana_info()
        
        # 获取玩家 ID
        pid = self.tracker.friendly_player_id or 1
        opp_id = 2 if pid == 1 else 1

        # 获取游戏状态数据
        hand_cards = self.tracker.get_my_hand()
        my_minions = self.tracker.get_my_board()
        enemy_minions = self.tracker.get_opp_board()
        my_hero = self.tracker.get_hero_state(pid)
        enemy_hero = self.tracker.get_hero_state(opp_id)
        choices = self.tracker.get_choices()
        
        # 只有在调度阶段才给带有描述的牌库，平时只给名字
        is_mulligan = (game_phase == "MULLIGAN")
        my_deck = self.tracker.get_my_deck(include_details=is_mulligan)

        # 聚合状态
        if hand_cards or game_phase == "MULLIGAN" or my_minions or choices:

            # 使用简单的格式显示在 Overlay Status 区域
            display_msg = f"阶段: {game_phase} | 回合: {turn} | 法力: {current_mana}/{resources_max}\n"
            display_msg += f"手牌: {len(hand_cards)} | 牌库: {len(my_deck)} | 场面: 我({len(my_minions)}) vs 敌({len(enemy_minions)})"
            if choices:
                display_msg += f"\n[!] 发现/抉择: {len(choices)}"

            if self.overlay:
                self.overlay.root.after(
                    0, self.overlay.update_status, display_msg)
            else:
                self.log(display_msg)

            # 控制台保留详细信息
            print(f"[*] 状态详情: {hand_cards}")
            if my_deck:
                print(f"[*] 牌库详情: {my_deck}")

            return {
                "mana": current_mana,
                "game_phase": game_phase,
                "turn": turn,
                "max_mana": resources_max,
                "hand_cards": hand_cards,
                "initial_deck": self.tracker.initial_deck,
                "my_deck": my_deck,
                "my_minions": my_minions,
                "enemy_minions": enemy_minions,
                "my_hero": my_hero,
                "enemy_hero": enemy_hero,
                "choices": choices,
                "message": "Log update detected (via hslog)"
            }

        return None





    def decide_action(self, state):
        """请求 OpenAI 返回 JSON 格式的操作指令"""
        if not state:
            return None

        if state.get("game_over"):
            return None

        self.log("[*] AI 正在思考中...")

        # 针对 Mulligan 阶段的特殊 Prompt
        if state.get("game_phase") == "MULLIGAN":
            prompt = f"""
            你是一个炉石传说AI助手。现在是起手调度阶段 (Mulligan)。
            
            你的手牌: {state.get("hand_cards", [])}
            你的牌库剩余: {state.get("my_deck", [])}
            
            请决定替换掉哪些牌。通常保留低费随从，替换高费牌。
            请输出 JSON 响应，格式如下：
            {{
                "thought": "简短的一句话思考过程",
                "actions": [
                    {{ "type": "MULLIGAN_REPLACE", "hand_index": 0, "desc": "替换第1张" }},
                    {{ "type": "MULLIGAN_CONFIRM", "desc": "确认" }}
                ]
            }}
            注意：如果不替换任何牌，直接输出 MULLIGAN_CONFIRM。
            只输出 JSON。
            """
        else:
            # 常规回合 Prompt - 职业选手版 (Professional Player)
            prompt = f"""
            Role: You are a professional Hearthstone player aiming for the highest win rate.
            
            Current Game State (JSON):
            {json.dumps(state, ensure_ascii=False)}
            
            Your Strategic Priorities (in order):
            0. **DECK AWARENESS**: Check your 'my_deck' in the state JSON to know what cards are remaining.
            1. **HANDLE CHOICES**: If "choices" list is present and not empty, you MUST return a "CHOOSE" action immediately. Do not do anything else.
            2. **CHECK LETHAL**: Calculate if you can kill the enemy hero this turn (Board damage + Hand damage). If yes, IGNORE TRADING and go face.
            3. **SURVIVAL**: If your Health is low, prioritize Taunt, Healing, or Clearing the board.
            4. **VALUE TRADING**: If not lethal, make favorable trades (kill enemy minion while keeping yours alive).
            5. **TEMPO**: Spend as much Mana as possible. Develop the board.
            6. **FACE**: If no good trades, attack Enemy Hero.
            
            Rules:
            - Minions with "exhausted": true cannot attack (unless they have Rush/Charge, but assume mostly no).
            - Minions with "attack": 0 cannot attack.
            - You cannot target "immune" or "stealth" characters.
            
            Output Format:
            Provide a strictly valid JSON response.
            IMPORTANT: Your 'thought' content MUST be in CHINESE.
            {{
                "thought": "简短的中文战术思考。例如：'斩杀线不够，优先解场。' / '发现法术。'",
                "actions": [
                    {{ "type": "CHOOSE", "index": 0, "desc": "Pick option 0" }},
                    {{ "type": "PLAY_MINION", "hand_index": 0, "desc": "Play Card X" }},
                    {{ "type": "PLAY_TARGET", "hand_index": 0, "target_type": "enemy_hero", "desc": "Fireball to Face" }},
                    {{ "type": "PLAY_TARGET", "hand_index": 0, "target_type": "minion", "target_index": 0, "desc": "Buff my minion 0" }},
                    {{ "type": "ATTACK", "attacker_index": 0, "target_type": "minion", "target_index": 0, "desc": "My Minion 0 trades Enemy Minion 0" }},
                    {{ "type": "ATTACK", "attacker_index": 0, "target_type": "enemy_hero", "desc": "Go Face" }},
                    {{ "type": "HERO_POWER", "desc": "Use Hero Power" }},
                    {{ "type": "END_TURN", "desc": "End Turn" }}
                ]
            }}
            
            IMPORTANT: Return ONLY the JSON. No markdown formatting.
            """

        try:
            # 使用 requests 调用，保持与原版一致的兼容性 (用户指定)
            headers = {
                "Authorization": f"Bearer {self.config['API_KEY']}",
                "Content-Type": "application/json"
            }

            # 优先尝试 gemini-3-flash-preview (用户保留)
            model_name = "gemini-3-flash-preview"
            data = {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": "You are a JSON-only response bot. Output ONLY valid JSON."},
                    {"role": "user", "content": prompt}
                ]
            }

            print(f"[*] 调用模型: {model_name} (via requests)")

            response = requests.post(
                f"{self.config['BASE_URL']}/chat/completions",
                headers=headers,
                json=data,
                timeout=30
            )

            # 错误处理
            if response.status_code != 200:
                print(
                    f"[!] API Error: {response.status_code} - {response.text}")
                return None

            result = response.json()
            content = result['choices'][0]['message']['content']

            # 清理 Markdown
            content = content.replace("```json", "").replace("```", "").strip()
            if content.startswith("```json"):
                content = content[7:]
            if content.endswith("```"):
                content = content[:-3]

            plan = json.loads(content)
            return plan

        except Exception as e:
            print(f"[!] AI 决策失败: {e}")
            return None

    def get_scaled_coord(self, pos):
        """
        将相对坐标 (0.0-1.0) 转换为当前屏幕的绝对坐标。
        完全基于 pyautogui.size() 获取的实时分辨率，不依赖任何预设值。
        """
        if not pos:
            return (0, 0)

        x, y = pos
        screen_w, screen_h = pyautogui.size()

        # 确保输入是相对坐标
        if isinstance(x, float) and x <= 1.0 and isinstance(y, float) and y <= 1.0:
            return (int(x * screen_w), int(y * screen_h))
            
        # 如果还在传递整数坐标，打印警告并尝试直接使用（假设是为了向下兼容或调试）
        # 但原则上应该全部更新为相对坐标配置
        print(f"[WARN] 接收到绝对坐标 {pos}，建议在 config.json 中更新为相对坐标 (0.0-1.0)")
        return (int(x), int(y))


    def get_hand_card_pos(self, index, total_cards):
        """动态计算手牌坐标 (Relative)"""
        # 基础参数 (校准后)
        center_x = 0.469
        # 手牌 Y 轴基准 (校准后)
        y_pos = 0.935
        
        # 卡牌间距动态调整
        # 3张牌: 0.1 (宽)
        # 10张牌: 0.035 (窄，重叠)
        # 简单插值公式
        if total_cards <= 1:
            spacing = 0.0
            start_x = center_x
        else:
            # 经验参数：最大宽度约 0.307 (校准后)
            max_width = 0.307
            # 计算每张牌的间距，但不超过最大单卡宽度 0.1
            spacing = min(0.1, max_width / max(1, total_cards - 1))
            
            # 整体居中
            total_width = (total_cards - 1) * spacing
            start_x = center_x - total_width / 2.0
            
        x = start_x + index * spacing
        return (x, y_pos)

    def vision_verify_highlight(self, x, y, radius=30):
        """
        验证屏幕指定坐标周围是否有绿色/黄色高亮 (使用 OpenCV)
        """
        try:
            # 1. 截取小区域 (减少计算开销)
            left, top = int(x - radius), int(y - radius)
            width, height = radius * 2, radius * 2
            
            # 使用 PIL 或 PyAutoGUI 截图并转换为 OpenCV 格式
            screenshot = pyautogui.screenshot(region=(left, top, width, height))
            img = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
            
            # 2. 转换到 HSV 空间检测颜色
            # 炉石可操作卡的绿光高亮 (大致范围)
            hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
            
            # 绿色高亮范围 (Hue: 40-90, Sat: 50-255, Val: 50-255)
            lower_green = np.array([35, 100, 100])
            upper_green = np.array([85, 255, 255])
            mask_green = cv2.inRange(hsv, lower_green, upper_green)
            
            # 黄色高亮范围 (金色传说/部分抉择卡)
            lower_yellow = np.array([20, 100, 100])
            upper_yellow = np.array([35, 255, 255])
            mask_yellow = cv2.inRange(hsv, lower_yellow, upper_yellow)
            
            green_pixels = cv2.countNonZero(mask_green)
            yellow_pixels = cv2.countNonZero(mask_yellow)
            
            total_active = green_pixels + yellow_pixels
            print(f"   [Vision] 坐标({x},{y}) 检测到高亮像素: 绿={green_pixels}, 黄={yellow_pixels}")
            
            return total_active > 50  # 阈值：至少 50 个像素满足颜色要求
        except Exception as e:
            print(f"   [Vision] 验证出错: {e}")
            return True # 出错时默认跳过验证，防止阻塞

    def vision_verify_choice_ui(self):
        """
        验证屏幕中央是否出现了“发现/抉择”的蓝色横幅
        """
        try:
            screen_w, screen_h = pyautogui.size()
            # 截取屏幕中央一条带 (横跨大部分宽度，高度在 0.1-0.2 左右)
            left, top = int(screen_w * 0.2), int(screen_h * 0.1)
            width, height = int(screen_w * 0.6), int(screen_h * 0.15)
            
            screenshot = pyautogui.screenshot(region=(left, top, width, height))
            img = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
            hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
            
            # 炉石抉择横幅的深蓝色/紫色背景
            lower_blue = np.array([100, 50, 50])
            upper_blue = np.array([130, 255, 255])
            mask_blue = cv2.inRange(hsv, lower_blue, upper_blue)
            
            blue_pixels = cv2.countNonZero(mask_blue)
            print(f"   [Vision] 抉择检测: 蓝色像素={blue_pixels}")
            return blue_pixels > 2000 # 经验值：大横幅会有很多蓝色像素
        except Exception as e:
            print(f"   [Vision] 抉择UI验证出错: {e}")
            return True

    def _find_hand_card(self, idx, hand_size, coordinates, debug_mode):
        """
        找到指定手牌的物理中心坐标 (视觉辅助)
        """
        if hand_size > 0:
            hand_pos_rel = self.get_hand_card_pos(idx, hand_size)
            start_pos = self.get_scaled_coord(hand_pos_rel)
        else:
            hand_cards_pos = coordinates.get("HAND_CARDS", [])
            if idx < len(hand_cards_pos):
                start_pos = self.get_scaled_coord(hand_cards_pos[idx])
            else:
                return (0, 0)

        if debug_mode:
            return start_pos

        # [VISION] 校验并寻找由于间距偏差导致的“点歪”
        print(f"   [Vision] 正在尝试悬停第 {idx+1} 张手牌...")
        pyautogui.moveTo(start_pos[0], start_pos[1], duration=0.2)
        time.sleep(0.3)
        
        if self.vision_verify_highlight(start_pos[0], start_pos[1]):
            return start_pos
        
        # 左右微调扫描
        for offset in [-20, 20, -40, 40, -60, 60]:
            test_x = start_pos[0] + offset
            pyautogui.moveTo(test_x, start_pos[1], duration=0.1)
            time.sleep(0.1)
            if self.vision_verify_highlight(test_x, start_pos[1]):
                print(f"   [Vision] 在偏移量 {offset} 处修正了卡牌中心")
                return (test_x, start_pos[1])
        
        print(f"   [Warning] 无法确认卡牌高亮，将使用原计算坐标: {start_pos}")
        return start_pos


    def perform_mouse_actions(self, plan, hand_size=0):
        """
        将 JSON 指令转换为 PyAutoGUI 动作
        :param plan: AI 决策的计划
        :param hand_size: 当前手牌数量，用于动态计算坐标
        """
        if not plan or "actions" not in plan:
            return

        actions = [a['desc'] for a in plan['actions'] if 'desc' in a]
        thought = plan.get("thought", "No thought provided.")

        # 显示思考过程和决策
        info_msg = f"AI 思考:\n{thought}\n\n执行操作:\n{actions}"
        self.log(info_msg)

        print(f"DEBUG: {plan}")

        coordinates = self.config.get("COORDINATES", {})
        debug_mode = self.config.get("DEBUG_MODE", False)
        
        # 获取基准坐标配置
        board_center = coordinates.get("BOARD_CENTER", [0.5, 0.5])
        enemy_hero = coordinates.get("ENEMY_HERO", [0.5, 0.2])
        end_turn = coordinates.get("END_TURN", [0.8, 0.5])
        choice_cards = coordinates.get("CHOICE_CARDS", [])
        mulligan_cards = coordinates.get("MULLIGAN_CARDS", [])
        mulligan_confirm = coordinates.get("MULLIGAN_CONFIRM", [0.5, 0.75])

        for action in plan["actions"]:
            action_type = action.get("type")

            if action_type == "CHOOSE":
                # [VISION] 校验抉择界面是否准备好
                if not debug_mode:
                    if not self.vision_verify_choice_ui():
                        print("   [Vision] 未检测到抉择横幅，等待 0.5s...")
                        time.sleep(0.5)
                
                # 发现/选择：点击
                idx = action.get("index", 0)
                if not choice_cards:
                    choice_cards = [[0.25, 0.5], [0.50, 0.5], [0.75, 0.5]]

                if idx < len(choice_cards):
                    pos = self.get_scaled_coord(choice_cards[idx])
                    print(f"   -> 选择选项 {idx} | 坐标 {pos}")
                    if not debug_mode:
                        # 悬停一下确认高亮（可选，但通常抉择按钮也有高亮）
                        pyautogui.moveTo(pos[0], pos[1], duration=0.2)
                        pyautogui.click()

            elif action_type == "PLAY_MINION" or action_type == "PLAY_SPELL_AOE":
                idx = action.get("hand_index", 0)
                # [VISION] 视觉寻找卡牌 (包含计算和扫描)
                start_pos = self._find_hand_card(idx, hand_size, coordinates, debug_mode)
                end_pos = self.get_scaled_coord(board_center)
                
                print(f"   -> 正在操作: {action.get('desc')} | 坐标 {start_pos} -> {end_pos}")
                if not debug_mode:
                    # 已经在 start_pos 了，直接拖拽
                    pyautogui.dragTo(end_pos[0], end_pos[1], duration=0.8, button='left')

            elif action_type == "PLAY_TARGET":
                # 指向性法术/战吼
                idx = action.get("hand_index", 0)
                start_pos = self._find_hand_card(idx, hand_size, coordinates, debug_mode)
                
                # ... (目标识别逻辑同前，略命调整为相对坐标)
                target_type = action.get("target_type", "enemy_hero")
                target_idx = action.get("target_index", 0)
                
                end_pos = (0, 0)
                if target_type == "minion":
                    enemy_minion_pos_list = coordinates.get("ENEMY_MINIONS", [])
                    if target_idx < len(enemy_minion_pos_list):
                        end_pos = self.get_scaled_coord(enemy_minion_pos_list[target_idx])
                    else:
                        end_pos = self.get_scaled_coord(board_center) # Fallback
                else:
                    end_pos = self.get_scaled_coord(enemy_hero)

                print(f"   -> 指向性打出: {start_pos} -> {end_pos}")
                if not debug_mode:
                    # 已经在 start_pos 了，直接拖拽
                    pyautogui.dragTo(end_pos[0], end_pos[1], duration=0.5, button='left')
            
            elif action_type == "END_TURN":
                pos = self.get_scaled_coord(end_turn)
                print(f"   -> 结束回合 | 坐标 {pos}")
                if not debug_mode:
                    pyautogui.moveTo(pos[0], pos[1], duration=0.2)
                    pyautogui.click()
            
            elif action_type == "MULLIGAN_REPLACE":
                idx = action.get("hand_index", 0)
                if idx < len(mulligan_cards):
                    pos = self.get_scaled_coord(mulligan_cards[idx])
                    print(f"   -> 替换手牌 {idx} | 坐标 {pos}")
                    if not debug_mode:
                        pyautogui.click(pos[0], pos[1])

            elif action_type == "MULLIGAN_CONFIRM":
                pos = self.get_scaled_coord(mulligan_confirm)
                print(f"   -> 确认调度")
                if not debug_mode:
                    pyautogui.click(pos[0], pos[1])

            elif action_type == "HERO_POWER":
                hero_power_pos = coordinates.get("HERO_POWER", [0.6, 0.75]) # 默认值
                pos = self.get_scaled_coord(hero_power_pos)
                print(f"   -> 使用英雄技能")
                if not debug_mode:
                    pyautogui.click(pos[0], pos[1])

            elif action_type == "ATTACK":
                 # 简化版: 默认第一个随从打脸
                 # 实际需要完善 My Minions 相对坐标
                atk_idx = action.get("attacker_index", 0)
                my_minion_pos = coordinates.get("MY_MINIONS", [])
                
                start_pos = (0,0)
                if atk_idx < len(my_minion_pos):
                     start_pos = self.get_scaled_coord(my_minion_pos[atk_idx])
                
                end_pos = self.get_scaled_coord(enemy_hero)
                print(f"   -> 随从攻击: {start_pos} -> {end_pos}")
                if not debug_mode:
                    pyautogui.moveTo(start_pos[0], start_pos[1], duration=0.2)
                    pyautogui.dragTo(end_pos[0], end_pos[1], duration=0.5, button='left')


    def run(self):
        # 初始化文件指针
        log_path = self.config.get("LOG_PATH", "Power.log")
        if os.path.exists(log_path):
            # 从头读取日志以重建完整状态 (支持中途启动)
            self.last_tell = 0

        print(f"[*] 已重置日志指针 (Start: 0)，正在重建游戏状态...")
        print("[*] (如果是中途启动，请等待几秒钟让脚本跑完历史记录)")
        print("[*] (请在游戏中进行任意操作，例如查看手牌/表情，以触发日志更新)")
        
        self.log_overlay.update_status("等待游戏开始...")
        
        try:
            while True:
                # 1. 获取状态
                state = self.get_game_state()

                # 2. 如果有新状态，且需要操作
                if state:
                    if state.get("game_over"):
                        print("[*] 游戏结束，等待下一局...")
                        self.log_overlay.update_status("游戏结束")
                        time.sleep(5)
                        continue

                    # 检查是否为我的回合
                    is_my_turn = self.tracker.is_my_turn()

                    # 检测回合开始 (从 False 变为 True)
                    if is_my_turn and not self.last_is_my_turn:
                         print("[*] 回合开始！等待抽牌动画(3.5s)...")
                         self.log_overlay.update_status("回合开始 | 等待抽牌...")
                         time.sleep(3.5)
                         # 重新获取状态以确保手牌更新
                         state = self.get_game_state()
                         if not state: continue
                    
                    self.last_is_my_turn = is_my_turn
                    phase = state.get("game_phase")
                    
                    # 只有在我的回合，或者是起手调度阶段，才进行 AI 思考
                    if is_my_turn or phase == "MULLIGAN":
                        self.log_overlay.update_status(f"我的回合 | {phase}")
                        
                        # 3. 获取决策 JSON
                        plan = self.decide_action(state)

                        # 4. 执行鼠标操作 (传递手牌数量以计算动态坐标)
                        hand_size = len(state.get("hand_cards", []))
                        self.perform_mouse_actions(plan, hand_size=hand_size)
                    else:
                        self.log_overlay.update_status(f"对手回合 | {phase}")
                        # print("[*] 等待对手行动...") 

                    # 防止由于模拟数据的频繁触发导致疯狂操作，实际中Log读取不会这样
                    time.sleep(5)

                time.sleep(1)
        except KeyboardInterrupt:
            print("\n[*] 用户停止程序。")

if __name__ == "__main__":
    overlay = LogOverlay()
    app = HearthstoneAutoPilot(overlay=overlay)

    # 逻辑循环必须在后台线程运行，否则会阻塞 GUI
    t = threading.Thread(target=app.run, daemon=True)
    t.start()

    # GUI 必须在主线程运行
    overlay.mainloop()
