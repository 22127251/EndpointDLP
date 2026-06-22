import ahocorasick
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# Test with Vietnamese keywords
automaton = ahocorasick.Automaton()
word1 = "tài"
word2 = "phân"
automaton.add_word(word1, (word1, [("pol1", "denylist")]))
automaton.add_word(word2, (word2, [("pol2", "denylist")]))
automaton.make_automaton()

# Search in Vietnamese text
text = "tài liệu phân loại"
text_lower = text.lower()

print("Text:", repr(text))
print("Lower:", repr(text_lower))
print()

for end_idx, (word, entries) in automaton.iter(text_lower):
    start_idx = end_idx - len(word) + 1
    end_exc = end_idx + 1
    matched = text_lower[start_idx:end_exc]
    print(f"end_idx={end_idx}, word={repr(word)}, len(word)={len(word)}")
    print(f"  start_idx={start_idx}, matched={repr(matched)}, len={len(matched)}")
    print(f"  Match: {word == matched}")
    print()
