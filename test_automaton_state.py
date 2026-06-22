import ahocorasick

# Test if automaton state changes during iteration
automaton = ahocorasick.Automaton()
automaton.add_word("test1", ("test1", []))
automaton.add_word("test2", ("test2", []))
automaton.make_automaton()

text = "test1 test2 test1"
text_lower = text.lower()

# First pass
hits1 = []
for end_idx, (word, _) in automaton.iter(text_lower):
    hits1.append(word)

# Second pass - should be identical
hits2 = []
for end_idx, (word, _) in automaton.iter(text_lower):
    hits2.append(word)

print("First pass:", hits1)
print("Second pass:", hits2)
print("Match:", hits1 == hits2)
