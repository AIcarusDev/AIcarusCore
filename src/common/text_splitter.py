# 这是一个文本分割器模块，主要用于将长文本分割成更小的句子或段落，以便于处理和分析。
# 该模块提供了多种功能，包括保护书名号和省略号、
# 目前这个方法已经不再使用了，项目中的文本分割逻辑已经迁移到 focus_chat_mode 模块中，由llm自己决定是否需要分割文本，以及如何分割。
# 但是为了未来可能的复用和兼容性，这里保留了这个模块的代码。
import random
import re

import regex

from src.config import config

# --- 全局常量和预编译正则表达式 ---
# \p{L} 匹配任何语言中的任何种类的字母字符。
_L_REGEX = regex.compile(r"\p{L}")
# \p{Han} 匹配汉字。
_HAN_CHAR_REGEX = regex.compile(r"\p{Han}")
# \p{Nd} 匹配十进制数字字符。
_Nd_REGEX = regex.compile(r"\p{Nd}")

# 书名号占位符的前缀，用于在处理文本时临时替换书名号。
BOOK_TITLE_PLACEHOLDER_PREFIX = "__BOOKTITLE_"
# 省略号占位符的前缀
ELLIPSIS_PLACEHOLDER_PREFIX = "__ELLIPSIS_"
# 定义句子分隔符集合。
SEPARATORS = {"。", "，", ",", " ", ";", "\xa0", "\n", ".", "—", "！", "？"}
# 已知的以点号结尾的英文缩写词，用于避免错误地将缩写词中的点号作为句子结束符。
KNOWN_ABBREVIATIONS_ENDING_WITH_DOT = {
    "Mr.",
    "Mrs.",
    "Ms.",
    "Dr.",
    "Prof.",
    "St.",
    "Messrs.",
    "Mmes.",
    "Capt.",
    "Gov.",
    "Inc.",
    "Ltd.",
    "Corp.",
    "Co.",
    "PLC",
    "vs.",
    "etc.",
    "i.e.",
    "e.g.",
    "viz.",
    "al.",
    "et al.",
    "ca.",
    "cf.",
    "No.",
    "Vol.",
    "pp.",
    "fig.",
    "figs.",
    "ed.",
    "Ph.D.",
    "M.D.",
    "B.A.",
    "M.A.",
    "Jan.",
    "Feb.",
    "Mar.",
    "Apr.",
    "Jun.",
    "Jul.",
    "Aug.",
    "Sep.",
    "Oct.",
    "Nov.",
    "Dec.",
    "Mon.",
    "Tue.",
    "Wed.",
    "Thu.",
    "Fri.",
    "Sat.",
    "Sun.",
    "U.S.",
    "U.K.",
    "E.U.",
    "U.S.A.",
    "U.S.S.R.",
    "Ave.",
    "Blvd.",
    "Rd.",
    "Ln.",
    "approx.",
    "dept.",
    "appt.",
    "श्री.",  # 印地语中的 Shri.
}

# --- 辅助函数 ---


def is_letter_not_han(char_str: str) -> bool:
    """
    检查单个字符是否为“字母”且“非汉字”。
    例如拉丁字母、西里尔字母、韩文等返回True。
    汉字、数字、标点、空格等返回False。

    Args:
        char_str:待检查的单个字符。

    Returns:
        bool: 如果字符是字母且非汉字则为True，否则为False。
    """
    if not isinstance(char_str, str) or len(char_str) != 1:
        return False
    is_letter = _L_REGEX.fullmatch(char_str) is not None
    if not is_letter:
        return False
    is_han = _HAN_CHAR_REGEX.fullmatch(char_str) is not None
    return not is_han


def is_han_character(char_str: str) -> bool:
    r"""
    检查单个字符是否为汉字 (使用 Unicode \p{Han} 属性)。

    Args:
        char_str: 待检查的单个字符。

    Returns:
        bool: 如果字符是汉字则为True，否则为False。
    """
    if not isinstance(char_str, str) or len(char_str) != 1:
        return False
    return _HAN_CHAR_REGEX.fullmatch(char_str) is not None


def is_digit(char_str: str) -> bool:
    """
    检查单个字符是否为Unicode数字 (十进制数字)。

    Args:
        char_str: 待检查的单个字符。

    Returns:
        bool: 如果字符是Unicode数字则为True，否则为False。
    """
    if not isinstance(char_str, str) or len(char_str) != 1:
        return False
    return _Nd_REGEX.fullmatch(char_str) is not None


def is_relevant_word_char(char_str: str) -> bool:
    """
    检查字符是否为“相关词语字符”（即非汉字字母或数字）。
    此函数用于判断在非中文语境下，空格两侧的字符是否应被视为构成一个连续词语的部分，
    从而决定该空格是否作为分割点。
    例如拉丁字母、西里尔字母、数字等返回True。
    汉字、标点、纯空格等返回False。

    Args:
        char_str: 待检查的单个字符。

    Returns:
        bool: 如果字符是非汉字字母或数字则为True，否则为False。
    """
    if not isinstance(char_str, str) or len(char_str) != 1:
        return False
    if _L_REGEX.fullmatch(char_str):
        return not _HAN_CHAR_REGEX.fullmatch(char_str)
    return bool(_Nd_REGEX.fullmatch(char_str))


def is_english_letter(char: str) -> bool:
    """
    检查单个字符是否为英文字母（忽略大小写）。

    Args:
        char: 待检查的单个字符。

    Returns:
        bool: 如果字符是英文字母则为True，否则为False。
    """
    return "a" <= char.lower() <= "z"


def protect_book_titles(text: str) -> tuple[str, dict[str, str]]:
    """
    保护文本中的书名号内容，将其替换为唯一的占位符。
    返回保护后的文本和占位符到原始内容的映射。

    Args:
        text: 原始输入文本。

    Returns:
        tuple[str, dict[str, str]]: 一个元组，包含：
            - protected_text (str): 书名号被占位符替换后的文本。
            - book_title_mapping (dict): 占位符到原始书名号内容（含书名号本身）的映射。
    """
    book_title_mapping = {}
    book_title_pattern = re.compile(r"《(.*?)》")

    def replace_func(match: re.Match) -> str:
        placeholder = f"{BOOK_TITLE_PLACEHOLDER_PREFIX}{len(book_title_mapping)}__"
        book_title_mapping[placeholder] = match.group(0)
        return placeholder

    protected_text = book_title_pattern.sub(replace_func, text)
    return protected_text, book_title_mapping


def recover_book_titles(sentences: list[str], book_title_mapping: dict[str, str]) -> list[str]:
    """
    将句子列表中的书名号占位符恢复为原始的书名号内容。

    Args:
        sentences: 包含可能书名号占位符的句子列表。
        book_title_mapping: 占位符到原始书名号内容的映射。

    Returns:
        list[str]: 书名号占位符被恢复后的句子列表。
    """
    recovered_sentences = []
    if not sentences:
        return []
    for sentence in sentences:
        if not isinstance(sentence, str):
            recovered_sentences.append(sentence)
            continue
        for placeholder, original_content in book_title_mapping.items():
            sentence = sentence.replace(placeholder, original_content)
        recovered_sentences.append(sentence)
    return recovered_sentences


def protect_ellipsis(text: str) -> tuple[str, dict[str, str]]:
    """
    保护文本中的省略号，将其替换为唯一的占位符。
    匹配连续三个或更多点号，以及Unicode省略号字符。
    返回保护后的文本和占位符到原始内容的映射。

    Args:
        text: 原始输入文本。

    Returns:
        tuple[str, dict[str, str]]: 一个元组，包含：
            - protected_text (str): 省略号被占位符替换后的文本。
            - ellipsis_mapping (dict): 占位符到原始省略号字符串的映射。
    """
    ellipsis_mapping = {}
    ellipsis_pattern = re.compile(r"(\.{3,}|\u2026)")

    def replace_func(match: re.Match) -> str:
        placeholder = f"{ELLIPSIS_PLACEHOLDER_PREFIX}{len(ellipsis_mapping)}__"
        ellipsis_mapping[placeholder] = match.group(0)
        return placeholder

    protected_text = ellipsis_pattern.sub(replace_func, text)
    return protected_text, ellipsis_mapping


def recover_ellipsis(sentences: list[str], ellipsis_mapping: dict[str, str]) -> list[str]:
    """
    将句子列表中的省略号占位符恢复为原始的省略号字符串。

    Args:
        sentences: 包含可能省略号占位符的句子列表。
        ellipsis_mapping: 占位符到原始省略号字符串的映射。

    Returns:
        list[str]: 省略号占位符被恢复后的句子列表。
    """
    recovered_sentences = []
    if not sentences:
        return []
    for sentence in sentences:
        if not isinstance(sentence, str):
            recovered_sentences.append(sentence)
            continue
        for placeholder, original_content in ellipsis_mapping.items():
            sentence = sentence.replace(placeholder, original_content)
        recovered_sentences.append(sentence)
    return recovered_sentences


def split_into_sentences_w_remove_punctuation(original_text: str) -> list[str]:
    """
    将输入文本分割成句子列表。
    此过程包括：
    1. 保护书名号和省略号。
    2. 文本预处理（如处理换行符）。
    3. 基于分隔符将文本切分为初步的段落(segments)。
    4. 根据段落内容和分隔符类型，构建初步的句子列表(preliminary_final_sentences)，
       特别处理汉字间的空格作为分割点。
    5. 对初步句子列表进行可能的合并（基于随机概率和文本长度）。
    6. 对合并后的句子进行随机标点移除。
    7. 恢复书名号和省略号。
    8. 返回最终处理过的句子列表。

    Args:
        original_text: 原始输入文本。

    Returns:
        list[str]: 分割和处理后的句子列表。
    """
    text, local_book_title_mapping = protect_book_titles(original_text)
    text, local_ellipsis_mapping = protect_ellipsis(text)

    perform_book_title_recovery_here = True

    text = regex.sub(r"\n\s*\n+", "\n", text)
    text = regex.sub(r"\n\s*([—。.,，;\s\xa0！？])", r"\1", text)
    text = regex.sub(r"([—。.,，;\s\xa0！？])\s*\n", r"\1", text)

    def replace_han_newline(match: re.Match) -> str:
        char1 = match.group(1)
        char2 = match.group(2)
        if is_han_character(char1) and is_han_character(char2):
            return char1 + "，" + char2
        return match.group(0)

    text = regex.sub(r"(.)\n(.)", replace_han_newline, text)

    len_text = len(text)

    is_only_placeholder = False
    if local_book_title_mapping and text in local_book_title_mapping:
        is_only_placeholder = True
    if not is_only_placeholder and local_ellipsis_mapping and text in local_ellipsis_mapping:
        is_only_placeholder = True

    if len_text < 3 and not local_book_title_mapping and not local_ellipsis_mapping:
        stripped_text = text.strip()
        if not stripped_text:
            return []
        if len(stripped_text) == 1 and stripped_text in SEPARATORS:
            return []
        return [stripped_text]

    segments = []
    current_segment = ""
    i = 0
    while i < len(text):
        char = text[i]
        if char in SEPARATORS:
            can_split_current_char = True

            if char == ".":
                can_split_this_dot = True
                if (
                    0 < i < len_text - 1
                    and is_digit(text[i - 1])
                    and is_digit(text[i + 1])
                    or 0 < i < len_text - 1
                    and is_letter_not_han(text[i - 1])
                    and is_letter_not_han(text[i + 1])
                ):
                    can_split_this_dot = False
                else:
                    potential_abbreviation_word = current_segment + char
                    is_followed_by_space = i + 1 < len_text and text[i + 1] == " "
                    is_at_end_of_text = i + 1 == len_text
                    if potential_abbreviation_word in KNOWN_ABBREVIATIONS_ENDING_WITH_DOT and (
                        is_followed_by_space or is_at_end_of_text
                    ):
                        can_split_this_dot = False
                can_split_current_char = can_split_this_dot
            elif char == " " or char == "\xa0":
                if 0 < i < len_text - 1:
                    prev_char = text[i - 1]
                    next_char = text[i + 1]
                    if is_relevant_word_char(prev_char) and is_relevant_word_char(next_char):
                        can_split_current_char = False

            if can_split_current_char:
                if current_segment:
                    segments.append((current_segment, char))
                elif char not in [" ", "\xa0"] or char == "\n":
                    segments.append(("", char))
                current_segment = ""
            else:
                current_segment += char
        else:
            current_segment += char
        i += 1

    if current_segment:
        segments.append((current_segment, ""))

    filtered_segments = []
    for content, sep in segments:
        stripped_content = content.strip()
        if stripped_content:
            filtered_segments.append((stripped_content, sep))
        elif sep and (sep not in [" ", "\xa0"] or sep == "\n"):
            filtered_segments.append(("", sep))
    segments = filtered_segments

    preliminary_final_sentences = []
    current_sentence_build = ""
    num_segments = len(segments)
    for k, (content, sep) in enumerate(segments):
        current_sentence_build += content

        is_strong_terminator = sep in {"，", "。", ".", "！", "？", "\n", "—"}
        is_space_separator = sep in [" ", "\xa0"]

        append_sep_to_current = is_strong_terminator
        should_split_now = False

        if is_strong_terminator:
            should_split_now = True
        elif is_space_separator:
            if current_sentence_build:
                last_char_of_build_stripped = current_sentence_build.strip()
                if (
                    last_char_of_build_stripped
                    and is_han_character(last_char_of_build_stripped[-1])
                    and k + 1 < num_segments
                ):
                    next_content_tuple = segments[k + 1]
                    if next_content_tuple:
                        next_content = next_content_tuple[0]
                        if next_content and is_han_character(next_content[0]):
                            should_split_now = True
                            append_sep_to_current = False

            if not should_split_now:
                if (
                    current_sentence_build
                    and not current_sentence_build.endswith(" ")
                    and not current_sentence_build.endswith("\xa0")
                ):
                    current_sentence_build += " "
                append_sep_to_current = False

        if should_split_now:
            if append_sep_to_current and sep:
                current_sentence_build += sep

            stripped_sentence = current_sentence_build.strip()
            if stripped_sentence:
                preliminary_final_sentences.append(stripped_sentence)
            current_sentence_build = ""
        elif sep and not is_space_separator:
            current_sentence_build += sep
            if k == num_segments - 1 and current_sentence_build.strip():
                preliminary_final_sentences.append(current_sentence_build.strip())
                current_sentence_build = ""

    if current_sentence_build.strip():
        preliminary_final_sentences.append(current_sentence_build.strip())

    preliminary_final_sentences = [s for s in preliminary_final_sentences if s.strip()]

    intermediate_sentences_placeholders = []

    if not preliminary_final_sentences:
        if is_only_placeholder:
            intermediate_sentences_placeholders = [text]

    elif len(preliminary_final_sentences) == 1:
        s = preliminary_final_sentences[0].strip()
        if s:
            s = random_remove_punctuation(s)
        intermediate_sentences_placeholders = [s] if s else []

    else:
        final_sentences_merged = []
        original_len_for_strength = len(original_text)
        split_strength = 0.5
        if original_len_for_strength < 12:
            split_strength = 0.5
        elif original_len_for_strength < 32:
            split_strength = 0.7
        else:
            split_strength = 0.9
        actual_merge_probability = 1.0 - split_strength

        temp_sentence = ""
        if preliminary_final_sentences:
            temp_sentence = preliminary_final_sentences[0]
            for i_merge in range(1, len(preliminary_final_sentences)):
                current_sentence_to_merge = preliminary_final_sentences[i_merge]
                should_merge_based_on_punctuation = True
                if temp_sentence and (
                    temp_sentence.endswith("。")
                    or temp_sentence.endswith(".")
                    or temp_sentence.endswith("!")
                    or temp_sentence.endswith("?")
                    or temp_sentence.endswith("—")
                ):
                    should_merge_based_on_punctuation = False

                if random.random() < actual_merge_probability and temp_sentence and should_merge_based_on_punctuation:
                    # 检查是否需要添加空格
                    need_space = False
                    if temp_sentence and current_sentence_to_merge:
                        last_char = temp_sentence.strip()[-1] if temp_sentence.strip() else ""
                        first_char = current_sentence_to_merge.strip()[0] if current_sentence_to_merge.strip() else ""

                        # 如果前后都是非中文字符（如英文、俄文等），才添加空格
                        if (is_letter_not_han(last_char) or is_digit(last_char)) and (
                            is_letter_not_han(first_char) or is_digit(first_char)
                        ):
                            need_space = True

                    if need_space and not temp_sentence.endswith(" ") and not current_sentence_to_merge.startswith(" "):
                        temp_sentence += " "
                    temp_sentence += current_sentence_to_merge
                else:
                    if temp_sentence:
                        final_sentences_merged.append(temp_sentence)
                    temp_sentence = current_sentence_to_merge
            if temp_sentence:
                final_sentences_merged.append(temp_sentence)

        processed_temp = []
        for sentence_val in final_sentences_merged:
            s_loop = sentence_val.strip()
            if s_loop.endswith(",") or s_loop.endswith("，"):
                s_loop = s_loop[:-1].strip()
            if s_loop:
                s_loop = random_remove_punctuation(s_loop)
            if s_loop:
                processed_temp.append(s_loop)
        intermediate_sentences_placeholders = processed_temp

    sentences_after_book_title_recovery = []
    if perform_book_title_recovery_here and local_book_title_mapping:
        sentences_after_book_title_recovery = recover_book_titles(
            intermediate_sentences_placeholders, local_book_title_mapping
        )
    else:
        sentences_after_book_title_recovery = intermediate_sentences_placeholders

    final_sentences_recovered = []
    if local_ellipsis_mapping:
        final_sentences_recovered = recover_ellipsis(sentences_after_book_title_recovery, local_ellipsis_mapping)
    else:
        final_sentences_recovered = sentences_after_book_title_recovery

    return [s for s in final_sentences_recovered if s.strip()]


def random_remove_punctuation(text: str) -> str:
    """随机处理标点符号，模拟人类打字习惯

    Args:
        text: 要处理的文本

    Returns:
        str: 处理后的文本
    """
    result = ""
    text_len = len(text)

    for i, char in enumerate(text):
        if char == "。" and i == text_len - 1 and random.random() > 0.1:
            continue
        result += char
    return result


def protect_kaomoji(sentence: str) -> tuple[str, dict[str, str]]:
    """ "
    识别并保护句子中的颜文字（含括号与无括号），将其替换为占位符，
    并返回替换后的句子和占位符到颜文字的映射表。
    Args:
        sentence (str): 输入的原始句子
    Returns:
        tuple: (处理后的句子, {占位符: 颜文字})
    """
    kaomoji_pattern = re.compile(
        r"("
        r"[(\[（【]"
        r"[^()\[\]（）【】]*?"
        r"[^一-龥a-zA-Z0-9\s]"
        r"[^()\[\]（）【】]*?"
        r"[)\]）】"
        r"]"
        r")"
        r"|"
        r"([▼▽・ᴥω･﹏^><≧≦￣｀´∀ヮДд︿﹀へ｡ﾟ╥╯╰︶︹•⁄]{2,15})"
    )

    kaomoji_matches = kaomoji_pattern.findall(sentence)
    placeholder_to_kaomoji = {}

    for idx, match in enumerate(kaomoji_matches):
        kaomoji = match[0] if match[0] else match[1]
        placeholder = f"__KAOMOJI_{idx}__"
        sentence = sentence.replace(kaomoji, placeholder, 1)
        placeholder_to_kaomoji[placeholder] = kaomoji

    return sentence, placeholder_to_kaomoji


def recover_kaomoji(sentences: list[str], placeholder_to_kaomoji: dict[str, str]) -> list[str]:
    """
    根据映射表恢复句子中的颜文字。
    Args:
        sentences (list): 含有占位符的句子列表
        placeholder_to_kaomoji (dict): 占位符到颜文字的映射表
    Returns:
        list: 恢复颜文字后的句子列表
    """
    recovered_sentences = []
    for sentence in sentences:
        for placeholder, kaomoji in placeholder_to_kaomoji.items():
            sentence = sentence.replace(placeholder, kaomoji)
        recovered_sentences.append(sentence)
    return recovered_sentences


def get_western_ratio(paragraph: str) -> float:
    """计算段落中字母数字字符的西文比例
    原理：检查段落中字母数字字符的西文比例
    通过is_english_letter函数判断每个字符是否为西文
    只检查字母数字字符，忽略标点符号和空格等非字母数字字符

    Args:
        paragraph: 要检查的文本段落

    Returns:
        float: 西文字符比例(0.0-1.0)，如果没有字母数字字符则返回0.0
    """
    alnum_chars = [char for char in paragraph if char.isalnum()]
    if not alnum_chars:
        return 0.0

    western_count = sum(1 for char in alnum_chars if is_english_letter(char))
    return western_count / len(alnum_chars)


def process_llm_response(
    text: str,
    enable_kaomoji_protection: bool = config.focus_chat_mode.enable_kaomoji_protection,
    enable_splitter: bool = config.focus_chat_mode.enable_splitter,
    max_length: int = config.focus_chat_mode.max_length,
    max_sentence_num: int = config.focus_chat_mode.max_sentence_num,
) -> list[str]:
    """
    处理LLM的响应文本，包括可选的颜文字保护、文本清洗和句子分割。

    Args:
        text (str): 从LLM获取的原始文本。
        enable_kaomoji_protection (bool): 是否启用颜文字保护。
        enable_splitter (bool): 是否启用句子分割逻辑。
        max_length (int): 文本最大长度限制。
        max_sentence_num (int): 分割后的最大句子数量。

    Returns:
        list[str]: 处理后的句子列表。
    """
    if enable_kaomoji_protection:
        protected_text, kaomoji_mapping = protect_kaomoji(text)
    else:
        protected_text = text
        kaomoji_mapping = {}

    # 提取并移除可能由模型生成的、表示动作或状态的中文方括号内容
    pattern = re.compile(r"[\[](?=.*[一-鿿]).*?[\]）]")
    _extracted_contents = pattern.findall(protected_text)
    cleaned_text = pattern.sub("", protected_text)

    if not cleaned_text.strip():
        # 如果移除括号后文本为空，可以返回一个默认的静默或表示困惑的回复
        return ["..."]

    # 对主要为中文的文本进行长度检查
    if get_western_ratio(cleaned_text) < 0.1 and len(cleaned_text) > max_length:
        # 如果文本过长，返回一个表示“不想说太多”的简洁回复
        return ["话太多了，不想说。"]

    # 根据配置决定是否分割句子
    sentences = split_into_sentences_w_remove_punctuation(cleaned_text) if enable_splitter else [cleaned_text]

    # 检查分割后的句子数量是否超限
    if len(sentences) > max_sentence_num:
        return ["一次说太多了，我脑子处理不过来啦..."]

    # 恢复颜文字（如果之前保护了的话）
    if enable_kaomoji_protection:
        sentences = recover_kaomoji(sentences, kaomoji_mapping)

    return sentences
